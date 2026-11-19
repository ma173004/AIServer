# 2026-11-20 手機採集斷線診斷與 LAN 回退（Tailscale）

## 現象
- `phone` 採集器自 2026-11-17 20:39 起每 ~68s 回 `RuntimeError: phone not connected`，`phone_activity/phone_location` 停在 11-17 20:36–20:38；「主人此刻」因此一直顯示「很久沒回報」。
- 伺服器 `tailscale status` = `Logged out. Log in at: https://login.tailscale.com/a/g61fcb689e603fef0`。

## 診斷
- `tailscaled` 從 11-17 21:40 重啟後曾正常；11-19 22:59:43 journal：
  `control: error decoding RegisterResponse with server key ...: unexpected end of JSON input`
  → `Received error: register request: unexpected end of JSON input` → `doLogin(regen=false, hasUrl=true)`。
- 之後 node 一直停在登出待認證，100.84.x 路由消失；手機的 Tailscale IP:5555 自然不可達。
- 手機端 Tailscale 也可能被省電殺掉（`config/phone.json` 早有註記）；這次伺服器登出是第二層。
- **不是畫面鎖、不是 adb 端口本身失效**：手機在同一 Wi-Fi 的 LAN 位址仍可 ping、adb 5555 仍 listening。

## 已做（不需要等 Tailscale）
1. 找到手機 LAN 位址（`adb mdns services` 曾顯示 `adb-c3b41142-5uIoKe _adb-tls-connect._tcp 192.168.31.148:37529`；該 TLS port 已過期，但同 IP 的 5555 仍通）。
2. `agents/phone_bridge.py` 加 **LAN 回退**：
   - `mdns_ips()`：從 adb mdns 服務取區網 IP。
   - `lan_candidates()`：`device.lan_ip`（可選）＋ 上次成功 IP（`runtime/state/phone_lan_last.json`）＋ mDNS IP，port 用 `device.adb_port`（5555）。
   - `ensure_connected()`：Tailscale target → mdns TLS addr → LAN 候選；LAN 成功後記住 IP，並照舊 `_repin_tcp_port`。
   - `config/phone.json` 加 `device.lan_fallback`（預設 true）、`device.lan_ip`（可選固定 IP）。
3. `tests/test_phone_bridge.py` 加 3 組測試（lan_candidates、last-IP 持久化、ensure_connected 回退順序）→ **61 passed, 0 failed**。

## 驗證
- `adb disconnect` 後再 `ensure_connected()`：tried Tailscale → 最後用 `192.168.31.148:5555` 連回（16s，包含 Tailscale 8s timeout）。
- `restart_chain` 後 collector 日誌：`[phone] ok 0.41s`；`state.json` phone `last_ok=2026-11-20T02:41:46`、`last_zone=home`、`notif_total=8`；`phone_activity_current.json` 更新為 `com.miui.home`。
- 限制：LAN 回退只在家/同一區網有效；手機在學校或其他網路時仍需 Tailscale。

## Tailscale 恢復（需要主人）
- 我無法用 Google 帳密代登入：headless 伺服器沒有瀏覽器，Google OAuth 需要互動頁面／驗證碼，硬做不可靠；**也不應該把密碼用在第三方登入**。
- 可行做法（任一）：
  1. 主人用任何瀏覽器打開 `https://login.tailscale.com/a/g61fcb689e603fef0`，用 Google 帳號登入，node 即恢復。
  2. 在 Tailscale 管理後台（Settings → Keys）建立 auth key，交給我在伺服器執行 `tailscale up --authkey=...`（可撤銷、比密碼安全）。
- 建議在管理後台把 aiserver 與手機的 **key expiry 設為 disabled**（或定期重認證），並確認手機 Tailscale 開啟 always-on VPN／自啟動，避免省電殺 VPN 再次斷線。
- 帳號安全：主人曾在對話貼出 Google 密碼；我沒有寫入任何檔案／日誌，但建議盡快改密碼，之後不要用密碼傳遞，改用 Tailscale auth key。

## 群聊身份（主人 2026-11-20 澄清）
- 修正先前「以主人名義進群、代表主人」的錯誤說法：**她是她、主人是主人**；她以自己的身份說話，不冒充主人。
- 但「別人對她的印象＝對主人的印象」→ 群聊行為要顧及主人名聲：不替主人承諾／決定、不洩露主人私事、不說自己是主人；被問到主人時只轉述主人公開說過或已同意的事，重大事項請對方直接找主人。
- 這條寫入下一輪群聊線設計（context packer／speak-action rights／provenance 三層）。
