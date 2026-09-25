# L3B Architecture Record

Tài liệu đặc tả kiến trúc hệ thống Multi-Agent L3B (A2A + MCP Gateway) theo chuẩn đánh giá của cuộc thi. Các quyết định thiết kế tập trung vào tính kiểm chứng, tuân thủ contract và nguyên tắc Least Privilege.

## 1. System overview

Luồng xử lý từ input case đến khi phát hành output JSON và observable trace:

```text
Input (Case JSON)
       │
       ▼
Coordinator Agent ─────────────► emit("case_received")
       │
       ▼ (task_assigned)
Entity Resolution Agent ◄──────► MCP [get_customer_history, get_order]
       │
       ▼ (handoff: ENTITY_RESOLVED)
Specialist Agents (Parallel Investigation):
  ├─ Order Agent ──────────────► MCP [get_order_items, get_product_context]
  ├─ Shipment Agent ───────────► MCP [get_shipment_summary, get_sellers]
  ├─ Payment Agent ────────────► MCP [get_order_payments, get_payment_timeline, get_refund_timeline]
  └─ Policy Agent ─────────────► MCP [get_policy]
       │
       ▼ (data & evidence aggregation)
Conflict Resolver Agent ───────► Phân xử bất đồng dữ liệu (precedence: SYSTEM > CARRIER > CLAIM)
       │
       ▼ (policy_decided)
Verifier Agent ────────────────► Kiểm tra Invariants & Schema contracts
       │
       ▼ (verification_completed)
Coordinator Agent ─────────────► Ghi output JSON & emit("case_finalized")
```

- Mọi tool call tới MCP Gateway đều được wrap trong `InvestigationContext` để cache theo `(case_id, tool_name, args)` và ghi nhận `tool_result_consumed` vào trace.
- Traces chỉ ghi các sự kiện quan sát được (`event_type`), không ghi prompt bí mật hay chain-of-thought.

---

## 2. Agent ownership (Phân quyền & Trách nhiệm)

Áp dụng chặt chẽ nguyên tắc **Least Privilege** (Quyền tối thiểu) — mỗi agent chỉ được cấp quyền gọi các công cụ thuộc thẩm quyền:

| Actor | Input | Trách nhiệm | Tool permission | Output / Handoff |
| :--- | :--- | :--- | :--- | :--- |
| **Coordinator** | Case input JSON | Khởi tạo quy trình, điều phối task, giám sát timeout và finalize output | Không gọi MCP trực tiếp | Giao việc cho `entity_agent`, nhận kết quả cuối từ `verifier` |
| **Entity Resolver** | `candidate_order_ids`, `claimed_order_id`, `customer_unique_id_hint` | Xác thực order thực tế, loại trừ dummy candidates, định danh customer | `get_customer_history`, `get_order` | `entity_resolution`, `customer_context` → Handoff `ENTITY_RESOLVED` |
| **Order Agent** | `order_id` đã resolve | Thu thập danh mục sản phẩm, sellers liên quan, ngữ cảnh mặt hàng | `get_order_items`, `get_product_context` | `item_ids`, thông tin chi tiết mặt hàng & giá |
| **Shipment Agent** | `order_id` đã resolve | Đánh giá tiến độ giao hàng, phân định trễ hạn do seller hay logistics | `get_shipment_summary`, `get_sellers` | `shipment_analysis` (verdict: `on_time`, `seller_delay`, `logistics_delay`, ...) |
| **Payment Agent** | `order_id` đã resolve | Đối soát thanh toán, phát hiện duplicate/mismatch, tra cứu timeline hoàn tiền | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | `payment_analysis` (verdict, captured, refunded, refundable totals) |
| **Policy Agent** | `policy_version`, primary issue | Áp dụng chính sách sàn để định mức bồi hoàn, hành động khắc phục | `get_policy` | Quy tắc bồi hoàn, bên chịu trách nhiệm (`responsible_parties`) |
| **Conflict Resolver** | Báo cáo từ tất cả specialists & claims | Phát hiện và giải quyết mâu thuẫn giữa thông tin khách khai và dữ liệu hệ thống | Không gọi tool | `data_conflicts`, `root_cause_analysis` |
| **Verifier** | Bản thảo output toàn diện | Kiểm tra 100% contracts schema, invariants logic, correlation ID | Không gọi tool | Emit `verification_completed`, xác nhận release output |

---

## 3. Entity resolution và A2A protocol

- **Candidate Evaluation & Rejection**:
  - So khớp `candidate_order_ids` với dữ liệu trả về từ `get_customer_history`.
  - Các candidate dạng chuỗi tạm (`candidate-XXX`) hoặc không thuộc lịch sử mua hàng của khách hàng bị phân loại dứt khoát vào `rejected_candidates`.
  - Candidate hợp lệ, khớp với `claimed_order_id` và tồn tại trong cơ sở dữ liệu Olist được gán vào `resolved_order_ids`.
- **Confidence Threshold**:
  - Gán `confidence = 0.95` khi đơn hàng được xác thực qua customer history và order metadata.
  - Gán `confidence = 0.50` và trạng thái `not_found` nếu không tìm thấy dữ liệu đơn hàng.
- **Correlation & Message Envelope**:
  - Mọi event và payload nội bộ đều mang `case_id` làm khóa tương quan duy nhất (Correlation ID).
  - Không có trạng thái dùng chung (state sharing) giữa các case khác nhau.
- **Tránh vòng lặp & Timeout**:
  - Luồng A2A tuân thủ Directed Acyclic Graph (DAG): `Entity → Specialists → Conflict → Verifier`.
  - Sử dụng `asyncio.gather` song song cho các specialist với timeout cố định 300s của gateway client.

---

## 4. Evidence và conflict lifecycle

- **Validation & Provenance**:
  - Mọi phản hồi từ MCP Gateway được kiểm tra hợp lệ theo contract `mcp-evidence-response-v1.schema.json`.
  - Trích xuất `evidence_ref` duy nhất (dạng `ev_[A-Za-z0-9_-]{20,96}`) và lưu vào danh sách `ctx.evidence_refs`.
  - Tuyệt đối không tự sinh hoặc tái sử dụng `evidence_ref` chéo giữa các case hoặc run khác nhau (ngăn chặn Hard Gate vi phạm Provenance).
- **Source Selection Precedence (Thứ tự ưu tiên nguồn tin)**:
  1. *Authoritative System / Gateway records* (mốc giao dịch, trạng thái đơn hàng).
  2. *Carrier / Logistics tracking events* (mốc thời gian bưu cục tiếp nhận, giao khách).
  3. *Seller shipping dispatch limits* (hạn chót giao hàng của người bán).
  4. *Customer dispute claims* (thông tin khiếu nại chủ quan).
- **Tool Result Consumed**:
  - Mỗi khi kết quả tool được một specialist agent tiêu thụ, hệ thống phát sinh event `tool_result_consumed` ghi nhận rõ `actor`, `tool_name` và `evidence_refs`.

---

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event / code |
| :--- | :---: | :--- | :--- |
| **MCP timeout** | 1 lần | Sử dụng dữ liệu fallback từ input context; đánh dấu `insufficient_evidence` | `tool_result_consumed(status="timeout")` |
| **Entity not found / ambiguous** | 0 lần | Đánh dấu `entity_resolution.status = "not_found"`, `rejected_candidates = all` | `handoff(decision_code="ENTITY_NOT_FOUND")` |
| **Source conflict** | 0 lần | Áp dụng quy tắc Precedence (System/Carrier > Claim); ghi nhận vào `data_conflicts` | `policy_decided(decision_code="CONFLICT_RESOLVED")` |
| **Tool error / Missing record** | 0 lần | Bắt ngoại lệ an toàn qua `call_tool_safe`; gán giá trị mặc định (vd: `refunded_total = 0.0`) | `tool_result_consumed(status="failed")` |

- **Query Budget & In-case Cache**:
  - Toàn bộ kết quả tool được lưu trong cache cục bộ của case theo `(tool_name, args)`.
  - Tuyệt đối không gọi lặp cùng một tool với cùng tham số trong 1 case.
  - Tránh quét rộng (broad scanning) trên các candidate không hợp lệ để bảo toàn điểm `efficiency`.

---

## 6. Verification invariants

Trước khi xuất output JSON, `Verifier` kiểm tra các ràng buộc bất biến (Invariants):
1. **Schema Invariant**: Output phải tuân thủ 100% `day09-l3b-output-v2.schema.json`.
2. **Case Scope Invariant**: `case_id` trong output phải khớp chính xác với input case.
3. **Evidence Invariant**: Danh sách `evidence_refs` không rỗng và chỉ chứa các mã `ev_*` thực sự được thu thập từ MCP audit của case hiện tại (tối đa 30 refs).
4. **Candidate Separation**: Giao giữa `resolved_order_ids` và `rejected_candidates` phải là tập rỗng (`set.intersection == ∅`).
5. **Financial Consistency**:
   - `recommended_refund_brl` phải bằng tổng `amount_brl` của các `refund_lines`.
   - `currency` luôn luôn là `"BRL"`.
   - Nếu `case_status == "no_action"`, `recommended_refund_brl` phải bằng `0.0`.
6. **Responsibility Consistency**:
   - Nếu `primary_issue == "late_delivery_seller"`, bên chịu trách nhiệm chính phải là `seller`.
   - Nếu `primary_issue == "late_delivery_logistics"`, bên chịu trách nhiệm phải là `logistics_provider`.
   - Nếu `primary_issue in ("valid_split_payment", "unsupported_claim")`, trách nhiệm thuộc về `customer` và `case_status == "no_action"`.

---

## 7. Reproducibility

- **Runtime & Environment**:
  - Python 3.12 (Virtual Environment `.venv`).
  - Dependencies: `mcp>=2.2.0`, `httpx2>=2.13`, `jsonschema>=4.26`, `pytest>=8.4`.
- **Thực thi và Kiểm thử**:
  - Kiểm tra đầu vào: `.venv/bin/day09 validate-inputs`
  - Khám phá công cụ: `.venv/bin/day09 mcp-tools`
  - Chạy toàn bộ 100 cases: `.venv/bin/day09 run`
  - Kiểm tra tính hợp lệ artifact: `.venv/bin/day09 validate`
  - Đóng gói submission: `.venv/bin/day09 package --output dist/submission.zip`
- **Tài nguyên & Bảo mật**:
  - Concurrency: Sequential case processing (1 case tại một thời điểm, các specialist bên trong case chạy song song qua asyncio).
  - Tuyệt đối không nhúng Team API Key hay thông tin bí mật vào trace, output hay submission archive.
