# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Multi-agent workflow chạy tuần tự với coordinator điều phối — mỗi agent nhận task, gọi MCP, handoff cho agent tiếp theo và kết thúc bằng verification.

```text
                          ┌──────────────────────────┐
                          │   Coordinator / Router   │
                          └─────────────┬────────────┘
                                        │ (Handoff)
         ┌──────────────────────────────┼──────────────────────────────┐
         ▼                              ▼                              ▼
┌──────────────────┐           ┌──────────────────┐           ┌──────────────────┐
│ Order/Item Agent │           │  Payment Agent   │           │  Shipment Agent  │
└────────┬─────────┘           └────────┬─────────┘           └────────┬─────────┘
         │                              │                              │
         └──────────────────────────────┼──────────────────────────────┘
                                        │ (MCP Evidence Collector)
                                        ▼
                               ┌──────────────────┐
                               │   Policy Agent   │
                               └────────┬─────────┘
                                        │
                                        ▼
                               ┌──────────────────┐
                               │  Verifier Agent  │
                               └────────┬─────────┘
                                        │ (Validated Output)
                                        ▼
                                   [END OUTPUT]
```

Luồng dữ liệu:
1. Input gồm case_id, claimed_order_id, candidate_order_ids, claims, policy_version, customer_unique_id_hint
2. Mỗi agent sử dụng MCP tool được phân quyền để thu thập evidence
3. Evidence ref được trace ngay khi tool trả về (tool_result_consumed)
4. Conflicting sources được detect và ghi vào data_conflicts
5. Verifier kiểm tra cross-field consistency trước khi build output

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
|---|---|---|---|---|
| Coordinator / Router | case candidates, claimed_order_id | Entity resolution — resolve order từ candidates; gọi get_order_details; customer context; điều phối handoff | get_order_details, get_customer_history | resolved/rejected orders, customer context, task_assigned events |
| Order/Item Agent | resolved_order_ids | Gọi get_order_items cho mỗi order; gọi get_product_details nếu scope yêu cầu | get_order_items, get_product_details | item_ids, seller_ids |
| Payment Agent | order_ids | Gọi get_payment_details, get_refund_details; tính captured_total, refunded_total, refundable_total | get_payment_details, get_refund_details | payment_verdict, totals, payment_refs |
| Shipment Agent | order_ids | Gọi get_shipment_details; xác định verdict (on_time/seller_delay/lost/returned/...); ghi nhận late_seller_ids | get_shipment_details | shipment_verdict, late_seller_ids, timeline_complete, shipment_ids |
| Policy Agent | policy_version, all evidence | Gọi get_policy_info; xác định primary_issue, data conflicts, claim assessments | get_policy_info | primary_issue, conflicts, claim verdicts |
| Verifier Agent | tất cả analysis output | Kiểm tra consistency; tính confidence tổng hợp; assembly output đúng schema; trace verification_completed | Không gọi tool | output dict + verification_completed trace |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

## 3. Entity resolution và A2A protocol

- **Coordinator xử lý**: Entity resolution là trách nhiệm của Coordinator/Router. Duyệt tuần tự candidate_order_ids, gọi `get_order_details` cho mỗi candidate. Candidate có `order_status` hợp lệ → resolved; ngược lại → rejected.
- **Fallback**: Nếu claimed_order_id không nằm trong resolved, thử gọi trực tiếp `get_order_details`. Nếu fail → thêm vào rejected.
- **Confidence**: 0.9 nếu claimed_order_id là resolved duy nhất; 0.65 nếu resolved nhiều; 0.25 nếu ambiguous.
- **Handoff flow**: Coordinator handoff → Order/Item Agent → Payment Agent → Shipment Agent → Policy Agent → Verifier Agent. Tuần tự, mỗi agent chuyên biệt hóa một domain.
- **Tránh vòng lặp**: Mỗi actor chỉ gọi 1 lần, handoff 1 chiều, không feedback loop giữa các agent.

## 4. Evidence và conflict lifecycle

- **Validate**: MCP response được gateway validate theo schema (contract.validate_evidence) ngay sau khi nhận.
- **Lưu evidence_ref**: Mỗi tool call thành công → `collected[evidence_ref] = full_response` + emit `tool_result_consumed`.
- **Chọn source theo policy**: Khi conflict (cùng field khác value giữa các evidence), chọn `latest_source_preferred`.
- **Map evidence vào claim**: Mỗi claim_assessment lấy evidence_refs từ domain tương ứng (shipment → late_delivery; payment/refund → payment/refund claims).
- **Không reuse**: collected chỉ chứa evidence từ case hiện tại, không cross-case.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
|---|---|---|---|
| MCP timeout | 0 (one-shot) | Bỏ qua candidate/evidence đó | tool_result_consumed với evidence_refs=[] |
| Entity not found/ambiguous | 0 | entity_status="ambiguous", confidence thấp | handoff → entity_ambiguous |
| Source conflict | 0 | Ghi vào data_conflicts, chọn source đầu tiên | verification_completed |
| Invalid specialist result | 0 | Bỏ qua field đó, dùng giá trị mặc định | tool_result_consumed (không emit nếu lỗi) |

**Query budget/cache strategy**: 
- Mỗi tool gọi tối đa 1 lần per order_id (không retry, không call thừa).
- Cache trong collected dict, không cache cross-case.
- `include_product_context` chỉ gọi get_product_details khi scope yêu cầu, tránh call vô ích.

## 6. Verification invariants

Kiểm tra trước finalize:
1. **Schema**: Mọi nested field đúng required (assessment, affected_entities, entity_resolution, ...)
2. **Entity scope**: resolved_order_ids không overlap với rejected_candidates
3. **Evidence ownership**: evidence_refs trong output phải là subset của collected keys
4. **Claim linkage**: Mỗi claim có evidence_refs phù hợp domain
5. **Timeline**: shipment_analysis.timeline_complete phản ánh có ít nhất 1 shipment record
6. **Payment/refund totals**: captured_total, refunded_total, refundable_total consistent
7. **Source precedence**: data_conflicts ghi rõ selected_source
8. **Confidence bounds**: [0,1] theo đúng schema
9. **Resolution actions**: Không duplicate actions, tối đa 8 items

## 7. Reproducibility

- **Model**: Không dùng ML model, hoàn toàn rule-based decision logic
- **Dependency**: pyproject.toml với setuptools, jsonschema, mcp, httpx2, python-dotenv
- **Concurrency**: Mỗi case chạy single-threaded async, không parallelism
- **Random seed**: Không dùng random
- **Lệnh chạy**: `day09 run` hoặc `pytest -q`
- **Giới hạn**: Mỗi case gọi tool tối đa ~10 calls (không tính product_context)
