# Hướng dẫn triển khai EGGS Jira MCP

Tài liệu hướng dẫn cài đặt và vận hành hệ thống `eggs-jira-mcp` cho Eggstech.

---

## 1. Thành phần hệ thống

Hệ thống triển khai bằng Docker Compose gồm hai service chạy song song:

1. **`mcp`**:
   - Chạy server FastMCP giao thức Streamable HTTP trên cổng 9000.
   - Cung cấp 25 công cụ nghiệp vụ `eggs_*` và 7 công cụ Jira chỉ đọc (`jira_search`, `jira_get_issue`, `jira_get_worklog`, `jira_batch_get_changelogs`, `jira_get_project_epic_hierarchy`, `jira_get_transitions`, `jira_search_fields`).
   - Mọi thao tác ghi của người dùng bắt buộc qua công cụ `eggs_*`. Mọi công cụ ghi Jira thô đều bị chặn.
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
- Có hostname nội bộ kèm chứng chỉ TLS hợp lệ (HTTPS) qua reverse proxy (Nginx, Traefik, Caddy hoặc Cloudflare Tunnel).
- Máy tính của các thành viên trong công ty kết nối được tới hostname này qua HTTPS.
- Kết nối mạng chiều đi (outbound) từ máy chủ tới:
  - `https://jira.eggstech.io.vn` (Jira Data Center).
  - `https://chat.googleapis.com` (Google Chat Webhook).

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
- `webhooks`: Điền URL Google Chat Webhook cho kênh thông báo chung của Lead và kênh riêng của từng dự án.

### Bước 4: Cấu hình biến môi trường

Tạo file `.env` từ file mẫu:

```bash
cp .env.example .env
```

Mở file `.env` và điền token của user bot:

```env
EGGS_BOT_TOKEN=your_jira_bot_personal_access_token_here
```

> **Lưu ý:** User bot (`eggs-bot`) cần thuộc nhóm `jira-administrators` trên Jira để có quyền đọc Project Role và cập nhật các trường sức khỏe với tham số tắt thông báo `notifyUsers=false`.

---

## 4. Khởi chạy hệ thống

Tại thư mục `deploy/eggs`, khởi chạy hai service bằng lệnh:

```bash
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

### 2. Gán QC Owner hàng loạt cho các Bug stand-alone

Chạy thử nghiệm (dry-run) để xem trước các thay đổi:

```bash
docker compose exec guardian eggs-guardian backfill-qc-owner --project <PROJECT_KEY> --dry-run
```

Chạy thật để cập nhật QC Owner vào Jira:

```bash
docker compose exec guardian eggs-guardian backfill-qc-owner --project <PROJECT_KEY>
```

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

# Build lại container image
docker compose build

# Khởi động lại dịch vụ
docker compose up -d
```
