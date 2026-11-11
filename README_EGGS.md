# Hướng dẫn triển khai EGGS Jira MCP

Tài liệu hướng dẫn cài đặt và vận hành hệ thống `eggs-jira-mcp` cho Eggstech.

---

## 1. Thành phần hệ thống

Hệ thống triển khai bằng Docker Compose gồm hai service chạy song song:

1. **`mcp`**:
   - Chạy server FastMCP giao thức Streamable HTTP trên cổng 9000.
   - Cung cấp các công cụ nghiệp vụ `eggs_*`, toàn bộ công cụ Jira đọc trên Jira Server/DC (loại trừ `cloud_only`), và 8 công cụ ghi an toàn (`jira_add_comment`, `jira_edit_comment`, `jira_add_watcher`, `jira_remove_watcher`, `jira_create_issue_link`, `jira_create_remote_issue_link`, `jira_remove_issue_link`, `jira_link_to_epic`).
   - Các công cụ ghi Jira thô làm vòng qua quy trình (tạo/sửa/xóa issue, transition, worklog, v.v.) đều bị chặn.
   - Xác thực người dùng bằng Personal Access Token (PAT) cá nhân qua header HTTP.

2. **`guardian`**:
   - Chạy tiến trình cron tự động bằng `supercronic`.
   - Đánh giá sức khỏe dự án mỗi giờ trong khung 08:00–19:00 từ Thứ 2 đến Thứ 6 (`eggs-guardian evaluate`).
   - Gửi báo cáo tổng hợp lên Google Chat lúc 08:30 từ Thứ 2 đến Thứ 6 (`eggs-guardian report`).
   - Nhắc nhở cập nhật công việc lúc 18:00 từ Thứ 2 đến Thứ 6 (`eggs-guardian remind`).
   - Khởi tạo Epic vận hành `[OPS]` vào ngày 1 đến ngày 3 đầu mỗi tháng (`eggs-guardian ops-epics`).
   - Sử dụng PAT của bot (`EGGS_BOT_TOKEN`) để ghi nhận trường sức khỏe và đọc danh sách thành viên Project Role.

---

## 2. Yêu cầu máy chủ (Prerequisites)

Máy chủ cần chuẩn bị các điều kiện sau:

- Đã cài đặt Docker Engine và Docker Compose (plugin `docker compose`).
- Có hostname nội bộ kèm chứng chỉ TLS hợp lệ (HTTPS) qua reverse proxy (Nginx, Traefik, Caddy hoặc Cloudflare Tunnel). Server chạy trên cùng host với Jira phía sau reverse proxy hiện có, publish cổng tới loopback `127.0.0.1:9000`.
- Máy tính của các thành viên trong công ty kết nối được tới hostname này qua HTTPS.
- Kết nối mạng chiều đi (outbound) từ máy chủ tới:
  - `https://jira.eggstech.io.vn` (Jira Data Center).
  - `https://chat.googleapis.com` (Google Chat Webhook).

### Cấu hình Reverse Proxy mẫu (Nginx)

Ví dụ cấu hình khối `location` trong Nginx proxy HTTPS hostname nội bộ tới `http://127.0.0.1:9000` (bắt buộc `proxy_buffering off` cho giao thức streaming MCP):

```nginx
server {
    server_name mcp-jira.internal.eggstech.io.vn;

    location / {
        proxy_pass http://127.0.0.1:9000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Bắt buộc tắt buffering cho MCP Streamable HTTP:
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
}
```

---

## 3. Cài đặt lần đầu

Thực hiện các bước sau tại thư mục `deploy/eggs`:

```bash
cd deploy/eggs
```

### Bước 1: Tạo file cấu hình từ file mẫu

Copy file cấu hình mẫu sang file sử dụng thực tế:

```bash
cp ../../src/mcp_atlassian/eggs/config.example.yaml ./config.yaml
```

### Bước 2: Dò tìm ID các trường tùy chỉnh (Custom Fields)

Chạy lệnh tự động dò tìm ID các customfield trên Jira:

```bash
uv run eggs-guardian resolve-fields
```

Điền các ID tìm được (ví dụ `customfield_10712`) vào mục `fields:` trong file `config.yaml`.

### Bước 3: Hoàn thiện thông tin dự án và webhook

Mở file `config.yaml` và cập nhật:
- `lead_user`: Tên đăng nhập Jira của Lead dự án.
- `projects`: Khai báo các dự án áp dụng quy trình, người chịu trách nhiệm (`default_lead_ba`, `triage_owner`, `qc_owner`).
- Webhook Google Chat **không** để trong `config.yaml`: tạo `webhooks.yaml` từ `webhooks.example.yaml` (cùng thư mục), điền `lead` (nhóm chung, nhận báo cáo tổng) và `projects: {KEY: URL}` (nhóm nội bộ của từng dự án). Chỉ nhận URL `https://chat.googleapis.com/...`. Guardian đọc lại file ở mỗi lượt chạy nên sửa xong không cần khởi động lại. Kiểm tra: `docker compose exec guardian eggs-guardian chat-test [--project KEY]` (in OK/LỖI từng nhóm, không in URL). Phải tạo file trước `docker compose up`, nếu không Docker tạo thư mục trùng tên.

### Bước 4: Cấu hình biến môi trường

Tạo file `.env` từ file mẫu:

```bash
cp .env.example .env
```

Mở file `.env` và điền token của user bot:

```env
EGGS_BOT_TOKEN=your_jira_bot_personal_access_token_here
```

> **Lưu ý:**
> - User bot (`eggsbot`) cần thuộc nhóm `jira-administrators` trên Jira để có quyền đọc Project Role và cập nhật các trường sức khỏe với tham số tắt thông báo `notifyUsers=false`.
> - Giá trị SHA1 của supercronic trong `deploy/eggs/Dockerfile.guardian` cần được kiểm tra đối chiếu với trang release v0.2.33 trước lần build đầu tiên.

---

## 4. Khởi chạy hệ thống

Tại thư mục `deploy/eggs`, thứ tự build container image là build `mcp` trước, sau đó build `guardian` (vì image guardian kế thừa `FROM eggs-jira-mcp:latest`):

```bash
docker compose build mcp
docker compose build guardian
docker compose up -d
```

Kiểm tra trạng thái các container:

```bash
docker compose ps
```

Container `mcp` có sẵn healthcheck tại endpoint `/healthz` để tự động kiểm tra trạng thái hoạt động.

---

## 5. Kết nối từ Client (AI Agent)

Các công cụ AI Agent (Antigravity, Claude Code, Cursor, OpenCode, Hermes) kết nối qua giao thức MCP Streamable HTTP.

### Cấu hình kết nối

- **URL:** `https://<internal-hostname>/mcp`
- **Headers:**
  - `Authorization: Token <JIRA_PAT_CỦA_BẠN>`

Ví dụ cấu hình trong file `mcp.json` / Claude Desktop / Antigravity:

```json
{
  "mcpServers": {
    "eggs-jira": {
      "url": "https://mcp-jira.internal.eggstech.io.vn/mcp",
      "headers": {
        "Authorization": "Token YOUR_PERSONAL_ACCESS_TOKEN"
      }
    }
  }
}
```

Mỗi người dùng sử dụng Personal Access Token của chính mình. Server sẽ tự động nhận diện danh tính và phân quyền dựa trên Project Role trong Jira.

---

## 6. Kiểm tra hoạt động

Sau khi cấu hình client, gọi thử các công cụ sau để kiểm tra:

1. **Kiểm tra phiên bản server:**
   - Gọi tool `eggs_version` (hoặc `jira_eggs_version`).
   - Kết quả trả về phiên bản server và phiên bản kit tương thích.

2. **Kiểm tra danh tính và vai trò:**
   - Gọi tool `eggs_whoami` (hoặc `jira_eggs_whoami`).
   - Kết quả trả về thông tin người dùng hiện tại và các vai trò (`dev`, `qc`, `lead_ba`) theo từng dự án.

---

## 7. Các lệnh một lần khi chuyển giao (Cutover)

Khi đưa hệ thống vào vận hành lần đầu trên dự án thực tế, thực hiện các lệnh sau:

### 1. Tạo Epic vận hành cho tháng hiện tại

```bash
docker compose exec guardian eggs-guardian ops-epics
```

Lệnh này tạo Epic `[OPS] Vận hành & Hotfix MM/YYYY` cho từng dự án đang hoạt động nếu chưa có.

### 2. Gán QC Owner hàng loạt (backfill-qc-owner)

Tự động gán QC Owner cho các issue chưa có theo quyết định Lead (không phụ thuộc vào search index của Jira):
- **Bug stand-alone**: Tất cả issue chưa Done.
- **Story**: Tất cả issue chưa Done.
- **Ad-hoc Task**: Chỉ những issue đang ở trạng thái sẵn sàng kiểm thử (`QC TO DO` hoặc `Test In Progress`).

Tham số `--types` cho phép Lead chạy từng loại riêng biệt (`bug`, `story`, `adhoc`, mặc định cả ba):

```bash
# Chạy thử nghiệm xem danh sách và số lượng
docker compose exec guardian eggs-guardian backfill-qc-owner --project <PROJECT_KEY> --dry-run

# Chạy cho từng loại cụ thể (ví dụ chỉ Bug)
docker compose exec guardian eggs-guardian backfill-qc-owner --project <PROJECT_KEY> --types bug

# Chạy thật cập nhật Jira
docker compose exec guardian eggs-guardian backfill-qc-owner --project <PROJECT_KEY>
```

### 3. Đóng băng các issue cũ không hoạt động (freeze-legacy)

Gán nhãn `legacy-frozen` cho các issue mở đã không cập nhật hơn 30 ngày (bỏ qua nếu có sub-task hoặc issue con trong Epic cập nhật trong 30 ngày qua):

```bash
# Chạy thử nghiệm xem trước danh sách issue stale
docker compose exec guardian eggs-guardian freeze-legacy --project <PROJECT_KEY> [--days 30] --dry-run

# Chạy thật để gán nhãn legacy-frozen
docker compose exec guardian eggs-guardian freeze-legacy --project <PROJECT_KEY> [--days 30]
```

Guardian sẽ bỏ qua việc tính cờ sức khỏe cho các issue có nhãn `legacy-frozen`. Khi có người làm việc lại trên issue (có worklog, comment hoặc chuyển trạng thái phát sinh sau thời điểm gắn nhãn), hệ thống sẽ **tự động gỡ nhãn `legacy-frozen`** trong lần đánh giá kế tiếp mà không cần thao tác thủ công. Cấu hình cửa sổ quét unfreeze qua `thresholds.unfreeze_window_days` (mặc định: 4 ngày).

---

## 8. Xem nhật ký (Logs)

Theo dõi nhật ký hoạt động của service:

```bash
# Xem log server MCP
docker compose logs -f mcp

# Xem log tiến trình cron Guardian
docker compose logs -f guardian
```

---

## 9. Quy trình cập nhật (Update)

Khi có phiên bản mới từ repository:

```bash
# Kéo mã nguồn mới nhất
git pull origin eggs/mvp

# Build lại container image theo đúng thứ tự (mcp trước, sau đó guardian)
docker compose build mcp
docker compose build guardian

# Khởi động lại dịch vụ
docker compose up -d
```

---

## 10. Công cụ lập kế hoạch và chuyển đổi Epic (Plan & Migrate)

Hệ thống hỗ trợ BA/Lead chuyển đổi tài liệu yêu cầu thành các Story/Dev Task chuẩn hóa trên Jira hoặc migrate các Epic cũ sang mô hình EGGS thông qua định dạng `plan.yaml` (`eggs-plan/v2`):

- **`eggs_plan_validate`**: Kiểm tra tính hợp lệ và chuẩn hóa cấu trúc `plan.yaml` đối chiếu với Jira (Component/Actor, tuỳ chọn Part, thành viên dự án, ngày hết hạn). Trả về cây công việc và danh sách câu hỏi cần làm rõ.
- **`eggs_plan_apply`**: Áp dụng kế hoạch `plan.yaml` lên Jira (hỗ trợ `dry_run=True` để xem trước thay đổi). Tự động tạo/cập nhật Epic, Story, Dev Task, Ad-hoc Task và liên kết `Blocks` giữa các luồng.
- **`eggs_migrate_epic`**: Trích xuất Epic hiện có trên Jira thành file `plan.yaml` mẫu kèm thống kê các thông tin còn thiếu (actor, due date, qc) để bổ sung trước khi chuẩn hóa.

### Vòng đời của Story (Story Workflow Lifecycle)

Quy trình chuẩn hóa Story:
1. **Open**: Story khởi tạo từ kế hoạch hoặc tạo mới.
2. **Analyzed**: Chuyển trạng thái qua transition `Analysis Done` khi Story có đầy đủ Component (Actor), QC Owner và Acceptance Criteria (AC). Nếu thiếu một trong ba thông tin này, Story giữ nguyên ở trạng thái `Open`.
3. **Ready For Dev**: Chuyển trạng thái khi tất cả Dev Task con đã có Part và estimate, kèm Due Date được chốt bởi BA/Lead.
4. **DEV To Do / Dev In Progress**: Dev thực hiện các phần việc.
5. **QC TO DO / Test In Progress**: QC thực hiện kiểm thử.
6. **Ready For UAT / Wait For Review / Done**: Khách hàng nghiệm thu và hoàn tất.

