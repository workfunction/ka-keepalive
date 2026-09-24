# ka-keepalive:Claude Code 的 prompt 快取保溫

一個在本機執行的小型 proxy。你離開座位時,它讓每個 Claude Code 工作階段的 **1 小時 prompt 快取**保持有效;回來送出下一則訊息時,就不必用快取寫入的價格把整段前綴重寫一次。

English: [README.md](README.md)

## 它做什麼、為什麼需要

- Claude Code 會把對話前綴快取 1 小時,閒置 60 分鐘後快取就失效。下一個請求得把整段前綴重新寫入,價格是一般輸入的 2 倍。40 萬 token 的工作階段,重寫一次要好幾美元。
- `ka_proxy.py` 在 `127.0.0.1:8787` 上,位於 Claude Code 與 `api.anthropic.com` 之間。所有請求原樣轉送,並把每個工作階段最後一個**主迴圈(main-loop)**請求**只留在記憶體裡**。
- 閒置 55 分鐘後,它用 `max_tokens: 0`、`stream: false` 重送那個請求。API 讀取快取(因此 1 小時 TTL 重新計時),但不產生任何輸出。**你的對話紀錄不會多出任何一輪**,模型也不會實際執行。
- 每次 ping 都會檢查兩件事:HTTP 200,以及快取讀取量至少達到預期前綴的 95%。暫時性失敗(帶 retry-after 的 429、529 overloaded)會重試一次;其他失敗就停止該工作階段的保溫。
- 每個工作階段保溫多久,由**動態上限**決定(見下文)。你也可以手動停止、恢復或延長。
- `STATE_DIR/ka.log` **只記錄中繼資料**:不含請求內容、prompt、輸出、標頭值或權杖。proxy 只綁定 127.0.0.1。

兩種接法:

| 模式 | 適用對象 | 方式 |
|---|---|---|
| **full**(預設) | CLI **和**桌面 App | 在 `~/.claude/settings.json` 設 `HTTPS_PROXY`。proxy 只替 `api.anthropic.com` 終結 TLS,使用的本機 CA 以名稱限制(name constraints)綁定這個主機,而且只透過 `NODE_EXTRA_CA_CERTS` 被信任。其他 CONNECT 目標一律是盲通道,絕不解密。CONNECT 必須帶 `Proxy-Authorization`(隨機權杖)。 |
| **cli-only** | 你指定的終端機工作階段 | `ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude`。不需要 CA,也不改設定檔。 |

## 需求

- Claude Code(CLI 和/或桌面 App)。
- **Python 3.10 以上**,只用標準函式庫。下限是 3.10:轉送串流逾時的判斷依賴 `socket.timeout` 就是 `TimeoutError`(3.10 起),儀表板也用到 `str.removeprefix`(3.9 起)。如果用較舊的 Python 啟動安裝程式,它會自動尋找較新的版本。
- **openssl**(只有 full 模式需要,用來建立本機 CA):macOS 內建(`/usr/bin/openssl`),Linux 用套件管理員安裝;Windows 上安裝程式會使用 **Git for Windows** 內附的版本(Windows 版 Claude Code 本來就需要 Git for Windows)。也可以用 `KA_OPENSSL=<路徑>` 指定。
- macOS:launchd(內建)。Linux:`systemd --user`(沒有的話請自行啟動,安裝程式會印出指令)。Windows:工作排程器(內建)。

## 安裝

把套件解壓縮到任意位置,在該資料夾開終端機,先做一次試跑。試跑會印出每個步驟,但不改任何東西:

```bash
python3 install.py --dry-run
```

接著安裝:

| 作業系統 | 指令 |
|---|---|
| macOS | `python3 install.py` |
| Linux | `python3 install.py` |
| Windows(PowerShell 或 cmd) | `py -3 install.py` |

安裝程式會做這些事:

1. 把 `bin/` 複製到 `~/.claude/ka/bin/`。macOS/Linux 會把指令碼的 `#!` 行固定成你執行安裝程式所用的直譯器;Windows 另外產生 `kactl.cmd`。
2. 把 `keepwarm` skill 安裝到 `~/.claude/skills/keepwarm/`,並填入這台機器的 `kactl` 指令。已有的 skill 會先移到 `~/.claude/ka/backup/`。不放在 `skills/` 底下,因為放在那裡會被載入成第二個 skill。
3. full 模式:建立 `~/.claude/ka/proxy.token`,以及 `~/.claude/ka/ca/` 裡的 CA。
4. 為 proxy 與儀表板註冊開機自動啟動:
   - macOS:LaunchAgents `com.ka-keepalive.proxy` / `com.ka-keepalive.dash`(`~/Library/LaunchAgents/`),RunAtLoad + KeepAlive。
   - Linux:`systemd --user` 單元 `ka-keepalive-proxy.service` / `ka-keepalive-dash.service`,`Restart=always`,登入時啟動。要在沒有登入工作階段時也執行,請用 `loginctl enable-linger`。
   - Windows:工作排程器工作 `ka-keepalive-proxy` / `ka-keepalive-dash`:登入時執行,使用 `pythonw.exe`(不開主控台視窗),失敗時自動重啟。
5. 印出 `settings.json` 的 env 區塊。只有加上 `--apply-settings` 才會寫入:先建立帶時間戳記的備份 `settings.json.bak-ka-<時間>`,再合併,其他鍵值全部保留。如果某個鍵已經是別人的值(例如公司的 proxy),它會拒絕寫入,什麼都不改。

選項:`--cli-only`、`--no-dashboard`、`--no-services`(只複製檔案)、`--port N`、`--dash-port N`、`--python 路徑`、`--apply-settings`、`--dry-run`、`--status`、`--uninstall [--purge]`。

## settings.json 的 env 設定(full 模式)

安裝程式會印出你機器上的實際值。`CLAUDE_CODE_SHELL_PREFIX` 讓 Bash 工具、hook 和 MCP 的子行程不經過 proxy:只有值和我們設定的完全相同時,它才會取消那個變數。

```json
"env": {
  "HTTPS_PROXY": "http://ka:<~/.claude/ka/proxy.token 裡的 32 位十六進位權杖>@127.0.0.1:8787",
  "NODE_EXTRA_CA_CERTS": "<家目錄>/.claude/ka/ca/ca.pem",
  "NO_PROXY": "localhost,127.0.0.1",
  "CLAUDE_CODE_SHELL_PREFIX": "<家目錄>/.claude/ka/bin/ka-shell-prefix.sh"
}
```

Windows 上兩個路徑都寫成用正斜線的磁碟代號路徑(`%USERPROFILE%` 底下的 `C:/.../.claude/ka/...`)。Node 的 `NODE_EXTRA_CA_CERTS` 接受這種寫法;`CLAUDE_CODE_SHELL_PREFIX` 由 Git Bash 執行(Windows 版 Claude Code 的 Bash 工具就跑在 Git Bash 上),它也接受。反斜線會被 shell 吃掉。

> **重要:新增的 env 會在執行中途套用到「正在執行」的工作階段;但移除 env 並「不會」讓它們恢復原狀。**
> 一旦 `HTTPS_PROXY` 寫進 settings.json,已經開著的工作階段就會開始走 proxy。之後刪掉這幾行,它們也不會回頭:看過這組 env 的工作階段,在它(或桌面 App)**重新啟動**之前,都會繼續走 proxy。proxy 停著的時候,這些工作階段就連不到 API。請先做完下面的冒煙測試,再把 env 寫進 settings.json。

如果啟動 Claude Code 的環境裡有小寫的 `https_proxy`,Claude Code 會先讀它而不是 `HTTPS_PROXY`,這個 proxy 就會被繞過。

## 第一次執行的冒煙測試

在動 settings.json 之前,先用「只對單一行程生效」的 env 開一個 CLI 工作階段試試。

1. 確認 proxy 已啟動:
   ```bash
   python3 install.py --status        # 服務已載入/執行中,8787 連接埠在監聽
   ```
2. 開一個新的終端機,用安裝程式印出的值啟動一個工作階段:
   ```bash
   # macOS / Linux
   HTTPS_PROXY='http://ka:<權杖>@127.0.0.1:8787' NODE_EXTRA_CA_CERTS=~/.claude/ka/ca/ca.pem \
     NO_PROXY=localhost,127.0.0.1 claude
   ```
   ```powershell
   # Windows PowerShell(變數只存在這個視窗,測完請關掉)
   $env:HTTPS_PROXY='http://ka:<權杖>@127.0.0.1:8787'; $env:NODE_EXTRA_CA_CERTS="$HOME/.claude/ka/ca/ca.pem"; $env:NO_PROXY='localhost,127.0.0.1'; claude
   ```
   cli-only 模式:`ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude`。
3. 送出一則訊息,應該正常回覆。
4. 在另一個終端機執行 `python3 ~/.claude/ka/bin/kactl status all`:這個工作階段應該顯示為 `active`,並有前綴大小與下次 ping 時間。
5. 確認沒有被拒絕:`grep -c proxy-auth-fail ~/.claude/ka/ka.log` 應該印出 `0`。這個事件就是 proxy 回的 407。
6. 以上都通過,再寫入 env 區塊(`python3 install.py --apply-settings`),然後重新啟動 Claude Code 與桌面 App。

## 日常使用

- **在工作階段裡**:用 `keepwarm` skill:`/keepwarm`(開啟)、`/keepwarm extend <小時>`(最多 24)、`/keepwarm stop`、`/keepwarm status`,也可以說「保溫」「延長保溫」「停止保溫」。proxy 沒在執行時,skill 會改用工作階段內每 55 分鐘一次的背景 tick。
- **在終端機**:`kactl`(`python3 ~/.claude/ka/bin/kactl ...`;Windows 用 `%USERPROFILE%\.claude\ka\bin\kactl.cmd ...`):
  - `kactl status all`:列出所有追蹤中的工作階段
  - `kactl stop <sid 前綴>|all` / `kactl resume <sid 前綴>|all`
  - `kactl extend <sid 前綴> <小時>`:超過動態上限後繼續 ping,最多 24 小時
  - `kactl off` / `kactl on`:全域開關
- **儀表板**:本機網頁,控制項和 kactl 相同。網址(含隨機權杖)是 `~/.claude/ka/dash.out` 裡最後一行 `kadash serving ...`;也可以用 `python3 install.py --status` 或 `python3 ~/.claude/ka/bin/kadash.py --url` 取得。
- **每日報表**:`python3 ~/.claude/ka/bin/ka_report.py [--day YYYY-MM-DD]`,列出 ping 次數、ping 成本,以及可能省下的重新預熱成本。
- **單則 prompt 關閉(選用)**:加上安裝程式印出的 `UserPromptSubmit` hook(`"<python>" "<家目錄>/.claude/ka/bin/ka_off_hook.py"`)之後,prompt 裡只要有一行以 `#ka-off` 開頭,就會停止該工作階段的 ping;句子中間提到 `#ka-off` 不算。

控制一律透過 `~/.claude/ka/` 裡的檔案(`stop/<sid>`、`off`、`extend/<sid>`),不走 HTTP;proxy 會在一個 30 秒的 tick 內套用。已停止、達上限或已過期的工作階段,在下一個真正的請求時重新開始。

## 動態上限怎麼算

proxy 對每個工作階段估兩個成本。**省下**的是重新預熱前綴的成本:(前綴 token 數 − 約 3 萬個共用 token)×(1 小時快取寫入價 − 讀取價)。**ping** 的是一次保溫的成本:讀一次前綴快取,加上約 3.8k 個未快取 token。接著在 1 到 12 之間挑出期望淨節省最大的閒置時數 H。計算用的是內建的「回來時間」分布:閒置 1 小時以上的時段中,有多少比例在 h 小時內結束,取自 471 段實際閒置紀錄。第 h 小時回來,省下「省下的成本 − 已付的 (h−1) 次 ping」;到 H 都沒回來,就付了 H 次 ping。如果沒有任何 H 是正值,上限就是 0,不保溫。硬上限是 12 小時(`KA_CAP_H_FABLE`、`KA_CAP_H_OPUS`)。價目表裡沒有的模型一律 4 小時(`KA_CAP_H_DEFAULT`)。`kactl extend` 可以覆蓋上限,最多 24 小時。每次送出真正的訊息,上限都重新計時。

## 停止/解除安裝

- 全部暫停:`kactl off`(工作階段仍然走 proxy,只是不再 ping)。
- 停止 proxy 服務:macOS `launchctl bootout gui/$UID/com.ka-keepalive.proxy`;Linux `systemctl --user stop ka-keepalive-proxy`;Windows `schtasks /End /TN ka-keepalive-proxy`。**請先移除 env 並重新啟動工作階段,再停止服務**,理由見上面的警告。
- 解除安裝:先 `python3 install.py --uninstall --dry-run`,確認後拿掉 `--dry-run` 再跑一次。它會停止並取消註冊兩個服務,從 settings.json 移除我們的 env 鍵(只移除值完全相符的鍵,移除前先備份),再刪除 `~/.claude/ka/bin` 與 skill(限我們安裝的)。`~/.claude/ka`(記錄檔、CA、權杖)預設保留,加 `--purge` 才一併刪除。**最後重新啟動所有 Claude Code 工作階段與桌面 App。**

## 疑難排解

| 症狀 | 原因/處理 |
|---|---|
| **407**/`ka.log` 出現 `proxy-auth-fail` | `HTTPS_PROXY` 裡的權杖和 `~/.claude/ka/proxy.token` 不一致:可能重新產生過,也可能打錯字。重跑 `python3 install.py` 印出目前的區塊,修正 settings.json 後重新啟動工作階段。如果桌面 App 不會送出網址裡的帳密,先改用 cli-only 模式。 |
| **連接埠被占用**(服務一直重啟,`proxy.err` 有 `Address already in use`) | 有別的程式,或舊版、服務名稱不同的同一個 proxy,占用了 8787。找出來(`lsof -iTCP:8787 -sTCP:LISTEN`;Windows `netstat -ano \| findstr 8787`)後停掉,或改用 `--port N` 安裝。用非預設連接埠時,env 區塊會多一個 `KA_PORT`。 |
| **CA 不被信任**(`self-signed certificate in certificate chain`、`unable to get local issuer certificate`) | `NODE_EXTRA_CA_CERTS` 沒設、設錯,或指向舊的 CA。檢查路徑後重新啟動工作階段(Node 只在啟動時讀取)。如果設了小寫 `https_proxy`,用的會是那個 proxy。要重新產生:`python3 ~/.claude/ka/bin/ka_ca.py --force`,執行中的 proxy 會自動換上新的 leaf 憑證。 |
| **429 用量上限** | ping 也是一般的 API 請求,同樣可能碰到速率或用量上限。帶 `retry-after` 的 429 會重試一次;否則該工作階段停止保溫(`kactl status all` 顯示 `verify-fail`),下一則真正的訊息會重新開始。可以用 `kactl stop` 或 `kactl off` 減輕負擔。 |
| **proxy 掛了,工作階段連不到 API** | 所有看過 `HTTPS_PROXY`(或 `ANTHROPIC_BASE_URL`)的工作階段,請求都會失敗。**最快的解法:把 proxy 重新啟動。** macOS `launchctl kickstart -k gui/$UID/com.ka-keepalive.proxy`;Linux `systemctl --user restart ka-keepalive-proxy`;Windows `schtasks /Run /TN ka-keepalive-proxy`;任何系統都可以手動執行 `python3 ~/.claude/ka/bin/ka_proxy.py`。要完全不再使用 proxy:先移除 env 鍵(`install.py --uninstall` 或手動編輯 settings.json),再把每個工作階段**重新啟動**。在新的終端機用 `claude --resume` 可以接回原本的對話。 |
| `kactl status all` 一直看不到某個工作階段 | 只追蹤帶 tools 的主迴圈請求,而且要至少有一則真正的訊息經過 proxy 之後才會出現。可以到 `ka.log` 找 `"event": "real"` 的紀錄。 |

記錄檔:`~/.claude/ka/ka.log`(JSONL,只有中繼資料),同一個資料夾還有 `proxy.out`/`proxy.err` 與 `dash.out`/`dash.err`。

## 已知限制/尚未驗證

- **Windows 沒有在實機上測過。** 路徑處理、工作排程器註冊(XML、登入觸發、`pythonw.exe`、失敗重啟)、`kactl.cmd`、Git Bash 對 `CLAUDE_CODE_SHELL_PREFIX` 的處理、Git for Windows 內附 openssl 的尋找,都只根據文件與靜態審查寫成。請先跑 `--dry-run`。如果 `CLAUDE_CODE_SHELL_PREFIX` 在 Windows 上讓 Bash 工具出錯,就拿掉這一個鍵:子行程會繼承 proxy 設定,proxy 執行期間仍然能用。
- **Linux** 的 `systemd --user` 單元沒有在實機上測過。
- **Windows 檔案權限**:macOS/Linux 上保護 `proxy.token`、`dash.token`、`ca/ca.key` 的 `0600`/`0700` 權限,在 Windows 上不存在,只能靠使用者設定檔資料夾的 ACL。請用 python.org 的 Python,不要用 Microsoft Store 版:工作排程器可能無法啟動 Store 版的 app execution alias(可用 `--python` 指定其他直譯器)。
- **桌面 App + `HTTPS_PROXY`**(包括它會不會送出網址裡的 `Proxy-Authorization` 帳密)只在 macOS 上確認過。
- **成本數字是依牌價換算的等值金額**,來自 `ka_proxy.py` 內建的小型價目表(`PRICES`)。訂閱方案下,它代表相對價值,不是實際扣款。
- **條款**:用 OAuth/訂閱登入時,重送自己的請求來維持快取,是否符合你帳號適用的條款,由你自行判斷並負責。
- 每個工作階段每閒置 1 小時 ping 一次,最多 8 個工作階段(`KA_MAX_SESSIONS`),同時最多 3 個 ping。保存的請求只在 proxy 的記憶體裡;proxy 重新啟動後就忘了,要等該工作階段下一則真正的訊息才會再記住。

## 套件內容

```
install.py               安裝程式(只用標準函式庫)
bin/ka_proxy.py          proxy + 保溫控制器
bin/kactl                命令列控制(Windows 另外產生 kactl.cmd)
bin/kadash.py            儀表板(127.0.0.1:8788,網址含權杖)
bin/ka_report.py         ka.log 每日摘要
bin/ka_ca.py             本機 CA + leaf 憑證(openssl)
bin/ka-shell-prefix.sh   CLAUDE_CODE_SHELL_PREFIX 包裝(bash;Windows 上是 Git Bash)
bin/ka_off_hook.py       #ka-off UserPromptSubmit hook(ka-off-hook.sh 為 shell 版)
bin/ka_service.py        Windows 服務啟動器(pythonw;輸出寫到 STATE_DIR/*.out|err)
skill/keepwarm/SKILL.md  工作階段內 skill 範本(安裝時填入 {{KACTL}})
STATUS-CONTRACT.md       status.json 與控制檔的約定(proxy、kactl、儀表板共用)
tests/                   python3 -m unittest discover -s tests(只用暫存資料夾與空閒連接埠)
```

調整參數(設在 proxy 服務的環境變數):`KA_PORT`、`KA_STATE_DIR`、`KA_PING_AFTER_S`(3300)、`KA_TTL_S`(3600)、`KA_TICK_S`(30)、`KA_MAX_SESSIONS`(8)、`KA_CAP_H_OPUS`/`KA_CAP_H_FABLE`(12)、`KA_CAP_H_DEFAULT`(4)、`KA_EXTEND_MAX_H`(24)。詳見 `ka_proxy.py` 的 `load_config()`。
