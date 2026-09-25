# L3B Architecture Record

Tài liệu ghi lại quyết định kiến trúc, phân quyền và cơ chế phối hợp tác tử (A2A) cho hệ thống điều tra khiếu nại thương mại điện tử Day09 L3B.

## 1. System overview

Luồng điều tra khép kín giữa các tác tử từ input đến output và trace audit:

```text
Input → Coordinator → Entity/Customer Agent → Order/Product Agent → Shipment Agent → Payment/Refund Agent → Policy & Conflict Agent → Verifier Agent → Output
   │           │                 │                    │                  │                   │                     │                     │            ▲
   │           ▼                 ▼                    ▼                  ▼                   ▼                     ▼                     ▼            │
   └──────── case_id ────────────┴────────────────────┴────── MCP Gateway Audit ─────────────┴─────────────────────┴─────────────────────┴────────────┘
                                                      (Observable Trace Events)
```

## 2. Agent ownership & Least Privilege

Hệ thống tuân thủ nguyên tắc đặc quyền tối thiểu (Least Privilege). Mỗi tác tử chỉ được cấp quyền gọi các công cụ MCP thuộc phạm vi chức năng chuyên biệt của mình:

| Actor | Input | Trách nhiệm | Tool permission | Output / Handoff |
| --- | --- | --- | --- | --- |
| `coordinator` | `case` object | Khởi tạo quy trình, giao nhiệm vụ, giám sát vòng đời | Không gọi MCP tool | Task assignment to `entity_agent`, finalized output |
| `entity_agent` | `candidate_order_ids`, `customer_unique_id_hint` | Phân giải candidate đơn hàng thực, trích xuất lịch sử khách hàng | `get_order`, `get_customer_history` | `resolved_order_id`, `rejected_candidates`, handoff to `order_agent` |
| `order_agent` | `resolved_order_id` | Khai thác thông tin sản phẩm, phân loại danh mục, danh sách người bán | `get_order_items`, `get_product_context` | `item_ids`, `seller_ids`, handoff to `shipment_agent` |
| `shipment_agent` | `resolved_order_id`, `seller_ids` | Phân tích timeline giao nhận, xác định độ trễ và quy trách nhiệm seller/carrier | `get_shipment_summary` | `shipment_verdict`, `late_seller_ids`, handoff to `payment_agent` |
| `payment_agent` | `resolved_order_id` | Kiểm tra chuỗi thanh toán, đối soát capture, kiểm tra sự cố hoàn tiền | `get_payment_timeline`, `get_refund_timeline` | `payment_verdict`, `captured_total_brl`, handoff to `policy_agent` |
| `policy_agent` | `claims`, `policy_version`, specialist findings | Áp dụng chính sách bồi hoàn, giải quyết mâu thuẫn nguồn (conflict resolution) | `get_policy` | `primary_issue`, `financial_resolution`, `data_conflicts`, handoff to `verifier` |
| `verifier` | Toàn bộ kết quả điều tra và danh sách `evidence_refs` | Kiểm tra toàn vẹn (invariants), schema compliance, đối soát số liệu | Không gọi MCP tool | `verification_completed` (decision: `passed`) |

## 3. Entity resolution và A2A protocol

* **Candidate Filtering & Selection**:
  * Đơn hàng hợp lệ trong dữ liệu Olist tuân thủ định dạng chuỗi 32 ký tự hex (MD5 format).
  * Ứng viên có định dạng giả định (ví dụ `candidate-xxx`) bị bác bỏ và đưa vào danh sách `rejected_candidates`.
  * `confidence` của quá trình entity resolution đạt 1.0 khi candidate khớp cấu trúc authoritative và được MCP `get_order` xác nhận thành công.
* **A2A Correlation Envelope**:
  * Mọi message và trace event liên kết chặt chẽ theo `case_id`.
  * Handoff tuần tự một chiều (`entity_agent` → `order_agent` → `shipment_agent` → `payment_agent` → `policy_agent` → `verifier`), ngăn ngừa triệt để hiện tượng deadlock hoặc vòng lặp đệ quy.

## 4. Evidence và conflict lifecycle

* **Evidence Validation**: Mọi phản hồi từ MCP Gateway được validate thông qua schema `day09-mcp-evidence-v1`. Trường `evidence_ref` được lưu trữ chính xác, không biến đổi, không tái sử dụng xuyên case (no cross-case leakage).
* **Trace Emission**: Tác tử tương ứng lập tức ghi nhận sự kiện `tool_result_consumed` với `evidence_refs` ngay khi tiêu thụ dữ liệu từ tool.
* **Source Conflict Resolution**:
  * Khi có độ lệch giữa trạng thái tổng quan của đơn hàng (`order_record`) và telemetry sự kiện vận chuyển chi tiết (`shipment_summary`), sự kiện vi mô từ hãng vận chuyển được ưu tiên theo mã giải quyết `carrier_event_precedence`.
  * Khi phát hiện sai lệch đối soát thanh toán (`payment_mismatch`), dữ liệu sổ cái chi tiết từ `payment_timeline` được áp dụng theo mã `mismatch_adjustment_required`.
  * Khi phát hiện đơn hàng đã hủy hoặc không khả dụng nhưng đã bị trừ tiền, trạng thái đơn hàng được ưu tiên để ra quyết định bồi hoàn theo mã `unfulfilled_paid_order`.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event / code |
| --- | ---: | --- | --- |
| MCP timeout | 1 lần | Thử lại với exponential backoff ngắn | `tool_retry` |
| Candidate không hợp lệ | 0 lần | Bác bỏ candidate, chuyển tiếp candidate hợp lệ | `candidate_rejected` |
| Refund timeline không tồn tại | 0 lần | Xác định chưa có giao dịch hoàn tiền phát sinh | Bỏ qua tool, ghi nhận `refund_timeline: empty` |
| Bất biến không thỏa mãn | 0 lần | Verifier báo lỗi và từ chối finalize output | `verification_failed` |

* **Query Budget & Efficiency**:
  * Mỗi case chỉ gọi chính xác các công cụ cần thiết theo phạm vi điều tra (khoảng 7–8 calls/case).
  * Không thực hiện quét brute-force trên danh sách candidate giả để bảo toàn điểm `efficiency` (5%).

## 6. Verification invariants

Trước khi xuất output cho mỗi case, `verifier` thẩm định các điều kiện bất biến:
1. **Schema Compliance**: Đạt 100% hợp lệ theo JSON Schema `day09-l3b-output-v2`.
2. **Provenance Invariant**: Mọi `evidence_ref` trong `evidence_refs` đều bắt đầu bằng tiền tố `ev_` và được cấp từ MCP audit của chính case đó.
3. **Financial Consistency**: Tổng số tiền hoàn lại trong `financial_resolution.recommended_refund_brl` phải bằng đúng tổng các dòng bồi hoàn trong `refund_lines`.
4. **Entity Scope**: `affected_entities.order_ids` chứa duy nhất đơn hàng đã được phân giải thành công.
5. **Action Consistency**: Hành động đề xuất trong `resolution_actions` đồng nhất với `recommended_action` quy định tại `EC_POLICY_V2`.

## 7. Reproducibility

* **Môi trường**: Python 3.11+, nền tảng Windows PowerShell.
* **Giao thức**: MCP HTTP Streaming qua thư viện `httpx2` và `mcp` SDK.
* **Kiểm thử & Đóng gói**:
  * Kiểm tra hợp lệ dữ liệu: `day09 validate-inputs`
  * Chạy điều tra tự động: `day09 run`
  * Xác thực kết quả: `day09 validate`
  * Đóng gói nộp bài: `day09 package --output dist/submission.zip`
