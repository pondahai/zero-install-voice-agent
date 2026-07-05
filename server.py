"""Voice agent server: static files + agent loop (tools) bridging browser <-> vLLM.

Browser sends one user utterance; this server runs the LLM+tool loop and
streams back SSE events: {"type":"delta"|"tool"|"tool_done"|"done"|"error", ...}
"""
import fnmatch
import functools
import html as htmllib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8010"))
MAX_ROUNDS = 8

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
OS_DESC = "Windows 電腦" if IS_WIN else ("macOS 主機" if IS_MAC else "Linux 主機")
SHELL_NAME = "PowerShell" if IS_WIN else "bash"

# ---------------------------------------------------------------- config

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
CONFIG = {
    "upstream": "http://localhost:8002",       # OpenAI 相容 API 位址（不含 /v1）
    "api_key": "",                             # 可留空；有值會帶 Authorization header
    "model": "",                               # 可留空 = 自動抓 /v1/models 第一個
}
try:
    with open(CONFIG_PATH, encoding="utf-8") as _f:
        CONFIG.update({k: v for k, v in json.load(_f).items() if k in CONFIG})
except (OSError, json.JSONDecodeError):
    pass


def save_config():
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, ensure_ascii=False, indent=2)


def upstream():
    return CONFIG["upstream"].rstrip("/")


def llm_headers():
    h = {"Content-Type": "application/json"}
    if CONFIG["api_key"]:
        h["Authorization"] = "Bearer " + CONFIG["api_key"]
    return h

def find_chrome():
    if IS_WIN:
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
        return next((p for p in candidates if os.path.exists(p)), None)
    if IS_MAC:
        p = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.exists(p):
            return p
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
        p = shutil.which(name)
        if p:
            return p
    return None


CHROME = find_chrome()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36")

SYSTEM_PROMPT = (
    f"你是一個跑在使用者{OS_DESC}上的語音助理 agent，你的回覆會被文字轉語音朗讀出來。"
    "請用台灣繁體中文、口語化的方式回答，簡短扼要，不要使用 markdown、條列符號、表情符號或特殊格式。"
    "你有以下工具：web_search 搜尋網路、web_fetch 讀取網頁內容、open_app 開啟程式或檔案或網址、"
    f"run_command 執行 {SHELL_NAME} 指令、find_files 尋找檔案。"
    "遇到需要即時資訊或你不確定的事實，先用 web_search；需要細節再用 web_fetch 讀結果網頁。"
    "執行完動作後用一句話回報結果。朗讀網址沒有意義，不要把網址唸出來，描述來源名稱即可。"
    "\n\n關於技能（skills）：技能是你累積的做事方法。開始做一件事之前，先看下面的技能清單，"
    "有相關技能就先用 load_skill 讀出完整步驟照著做。"
    "當使用者教你一個做法、糾正你的方式，或說「記住這個做法」時，用 save_skill 把步驟存起來"
    "（名稱要簡短、描述要一句話講清楚適用時機、內容寫具體步驟和用到的工具參數）。"
    "同名會覆寫，發現舊技能不對就直接更新它。"
    "另外，當你完成一件需要多個步驟摸索才成功的任務（例如試了幾種方法才找到答案），"
    "而且這個方法之後很可能重複用到時，在回報結果之後主動問使用者一句："
    "「要不要把這個做法記成技能？」使用者同意才呼叫 save_skill，不同意就不存。"
    "簡單一步就完成的事不用問。"
)

# ---------------------------------------------------------------- skills

SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")
os.makedirs(SKILLS_DIR, exist_ok=True)


def _skill_path(name):
    name = re.sub(r'[\\/:*?"<>|.]', "", str(name)).strip()[:60]
    if not name:
        raise ValueError("技能名稱無效")
    return os.path.join(SKILLS_DIR, name + ".md")


def skills_index():
    """每個技能檔第一行是描述；回傳「名稱: 描述」清單文字。"""
    lines = []
    for f in sorted(os.listdir(SKILLS_DIR)):
        if not f.endswith(".md"):
            continue
        try:
            with open(os.path.join(SKILLS_DIR, f), encoding="utf-8") as fh:
                desc = fh.readline().strip()
        except OSError:
            desc = ""
        lines.append(f"- {f[:-3]}: {desc}")
    return "\n".join(lines)


def tool_save_skill(name, description, content):
    path = _skill_path(name)
    existed = os.path.exists(path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(description.strip() + "\n\n" + content.strip() + "\n")
    return f"技能「{name}」已{'更新' if existed else '建立'}"


def tool_load_skill(name):
    path = _skill_path(name)
    if not os.path.exists(path):
        return f"沒有「{name}」這個技能。目前的技能:\n{skills_index() or '(無)'}"
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def tool_delete_skill(name):
    path = _skill_path(name)
    if not os.path.exists(path):
        return f"沒有「{name}」這個技能"
    os.remove(path)
    return f"技能「{name}」已刪除"

# ---------------------------------------------------------------- tools

def _strip_tags(s):
    s = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = htmllib.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _chrome_dump(url, budget_ms=6000):
    if not CHROME:
        raise RuntimeError("chrome.exe not found")
    r = subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--mute-audio",
         f"--virtual-time-budget={budget_ms}", f"--user-agent={UA}",
         "--dump-dom", url],
        capture_output=True, timeout=30)
    return r.stdout.decode("utf-8", "replace")


def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def _parse_bing(page):
    results = []
    for block in re.findall(r'<li class="b_algo"[\s\S]*?</li>', page):
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a>', block)
        if not m:
            continue
        url, title = m.group(1), _strip_tags(m.group(2))
        snip = ""
        sm = re.search(r"<p[^>]*>([\s\S]*?)</p>", block)
        if sm:
            snip = _strip_tags(sm.group(1))
        if url.startswith("http"):
            results.append({"title": title, "url": url, "snippet": snip[:300]})
    return results


def tool_web_search(query, max_results=5):
    url = "https://www.bing.com/search?q=" + urllib.parse.quote(query)
    results, source = [], ""
    if CHROME:  # 優先: 本機 Chrome headless（CDP 路線）
        try:
            results = _parse_bing(_chrome_dump(url))
            source = "chrome+bing"
        except Exception:
            pass
    if not results:  # 備援: 直接 HTTP 抓
        try:
            results = _parse_bing(_http_get(url))
            source = "http+bing"
        except Exception as e:
            return f"搜尋失敗: {e}"
    if not results:
        return "搜尋沒有找到結果"
    out = [f"搜尋結果 (來源 {source}):"]
    for i, r in enumerate(results[: int(max_results)], 1):
        out.append(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}")
    return "\n".join(out)


def tool_web_fetch(url):
    page = ""
    if CHROME:
        try:
            page = _chrome_dump(url, budget_ms=8000)
        except Exception:
            pass
    if not page:
        try:
            page = _http_get(url)
        except Exception as e:
            return f"讀取失敗: {e}"
    text = _strip_tags(page)
    return text[:6000] if text else "頁面沒有可讀文字"


def tool_open_app(target):
    if IS_WIN:
        # cmd start 會解析程式名/檔案路徑/網址三種目標
        subprocess.Popen(f'start "" "{target}"', shell=True)
    elif IS_MAC:
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(["xdg-open", target],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"已嘗試開啟: {target}"


def tool_run_command(command):
    shell_cmd = (["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
                 if IS_WIN else ["bash", "-c", command])
    try:
        r = subprocess.run(shell_cmd, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        return "指令執行超過 60 秒，已中止"

    def dec(b):
        for enc in ("utf-8", "cp950"):
            try:
                return b.decode(enc)
            except UnicodeDecodeError:
                continue
        return b.decode("utf-8", "replace")

    out = (dec(r.stdout).strip() + ("\n[stderr] " + dec(r.stderr).strip() if r.stderr.strip() else "")).strip()
    out = out or f"(無輸出, exit code {r.returncode})"
    return out[:4000]


def tool_find_files(pattern, root=None, max_results=30):
    root = root or os.path.expanduser("~")
    skip = {"node_modules", ".git", "__pycache__", "$recycle.bin", "windows", "appdata"}
    hits, deadline = [], time.time() + 12
    for dirpath, dirnames, filenames in os.walk(root):
        if time.time() > deadline:
            hits.append("(搜尋時間到，結果可能不完整)")
            break
        dirnames[:] = [d for d in dirnames if d.lower() not in skip and not d.startswith(".")]
        for f in filenames:
            if fnmatch.fnmatch(f.lower(), pattern.lower()):
                hits.append(os.path.join(dirpath, f))
                if len(hits) >= int(max_results):
                    return "\n".join(hits)
    return "\n".join(hits) if hits else f"在 {root} 底下找不到符合 {pattern} 的檔案"


TOOL_FUNCS = {
    "web_search": tool_web_search,
    "web_fetch": tool_web_fetch,
    "open_app": tool_open_app,
    "run_command": tool_run_command,
    "find_files": tool_find_files,
    "save_skill": tool_save_skill,
    "load_skill": tool_load_skill,
    "delete_skill": tool_delete_skill,
}

TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "用 Bing 搜尋網路，回傳標題、網址、摘要",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "搜尋關鍵字"},
            "max_results": {"type": "integer", "description": "最多回傳幾筆，預設 5"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "web_fetch",
        "description": "讀取指定網址的網頁內文（純文字）",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "完整網址"}},
            "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "open_app",
        "description": "在使用者的電腦上開啟程式、檔案或網址，例如 notepad、calc、一個路徑或 https:// 開頭的網址",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string", "description": "程式名、檔案路徑或網址"}},
            "required": ["target"]}}},
    {"type": "function", "function": {
        "name": "run_command",
        "description": f"在這台{OS_DESC}上執行 {SHELL_NAME} 指令並回傳輸出，60 秒逾時",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": f"{SHELL_NAME} 指令"}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "save_skill",
        "description": "把一個做事方法存成技能，之後的對話都會看到它。同名覆寫。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "技能名稱，簡短，例如「查天氣」"},
            "description": {"type": "string", "description": "一句話描述適用時機"},
            "content": {"type": "string", "description": "具體步驟，包含用到的工具和參數"}},
            "required": ["name", "description", "content"]}}},
    {"type": "function", "function": {
        "name": "load_skill",
        "description": "讀取一個技能的完整步驟",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "技能名稱"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "delete_skill",
        "description": "刪除一個過時或錯誤的技能",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "技能名稱"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "find_files",
        "description": "在資料夾底下遞迴尋找符合檔名樣式的檔案",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "檔名樣式，例如 *.pdf 或 report*.docx"},
            "root": {"type": "string", "description": "起始資料夾，預設為使用者家目錄"},
            "max_results": {"type": "integer", "description": "最多回傳幾筆，預設 30"}},
            "required": ["pattern"]}}},
]

# ---------------------------------------------------------------- agent loop

MODEL = None
HISTORY = [{"role": "system", "content": SYSTEM_PROMPT}]
LOCK = threading.Lock()


def get_model():
    global MODEL
    if CONFIG["model"]:
        return CONFIG["model"]
    if not MODEL:
        req = urllib.request.Request(upstream() + "/v1/models", headers=llm_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            MODEL = json.load(r)["data"][0]["id"]
    return MODEL


def llm_stream(messages, emit_delta):
    """Call vLLM streaming; forward content deltas; return (content, tool_calls)."""
    body = json.dumps({
        "model": get_model(), "messages": messages, "stream": True,
        "temperature": 0.7, "max_tokens": 2048,
        "tools": TOOLS_SCHEMA, "tool_choice": "auto",
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode("utf-8")
    req = urllib.request.Request(upstream() + "/v1/chat/completions", data=body,
                                 headers=llm_headers())
    content, calls = "", {}  # calls: index -> {id, name, arguments}
    with urllib.request.urlopen(req, timeout=600) as r:
        buf = b""
        while True:
            chunk = r.read1(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    continue
                try:
                    delta = json.loads(data)["choices"][0]["delta"]
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                if delta.get("content"):
                    content += delta["content"]
                    emit_delta(delta["content"])
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]
    tool_calls = [calls[i] for i in sorted(calls)] if calls else []
    return content, tool_calls


def run_agent(user_text, emit):
    """emit(dict) sends one SSE event to the browser."""
    idx = skills_index()
    HISTORY[0]["content"] = SYSTEM_PROMPT + (
        f"\n\n目前已學會的技能清單：\n{idx}" if idx else "\n\n目前還沒有任何技能。")
    HISTORY.append({"role": "user", "content": user_text})
    if len(HISTORY) > 60:  # 保留 system + 近期訊息
        del HISTORY[1:len(HISTORY) - 59]
    for _ in range(MAX_ROUNDS):
        content, tool_calls = llm_stream(HISTORY, lambda t: emit({"type": "delta", "text": t}))
        if not tool_calls:
            HISTORY.append({"role": "assistant", "content": content})
            return
        HISTORY.append({"role": "assistant", "content": content or None, "tool_calls": [
            {"id": c["id"] or f"call_{i}", "type": "function",
             "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
            for i, c in enumerate(tool_calls)]})
        for i, c in enumerate(tool_calls):
            name = c["name"]
            try:
                args = json.loads(c["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            emit({"type": "tool", "name": name, "args": args})
            fn = TOOL_FUNCS.get(name)
            try:
                result = fn(**args) if fn else f"未知工具: {name}"
            except Exception as e:
                result = f"工具執行失敗: {e}"
            emit({"type": "tool_done", "name": name, "summary": str(result)[:150]})
            HISTORY.append({"role": "tool", "tool_call_id": c["id"] or f"call_{i}",
                            "content": str(result)})
    emit({"type": "delta", "text": "工具使用次數達到上限，先停在這裡。"})
    HISTORY.append({"role": "assistant", "content": "（工具使用次數達到上限）"})

# ---------------------------------------------------------------- http server


class Handler(http.server.SimpleHTTPRequestHandler):
    def _sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/models":
            try:
                self._json({"model": get_model(), "tools": list(TOOL_FUNCS),
                            "chrome": bool(CHROME), "upstream": CONFIG["upstream"]})
            except Exception as e:
                self.send_error(502, f"upstream error: {e}")
            return
        if self.path == "/api/config":
            self._json({"upstream": CONFIG["upstream"], "model": CONFIG["model"],
                        "has_key": bool(CONFIG["api_key"])})
            return
        super().do_GET()

    def do_POST(self):
        global MODEL
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/config":
            if "upstream" in body and str(body["upstream"]).strip():
                CONFIG["upstream"] = str(body["upstream"]).strip()
            if "model" in body:
                CONFIG["model"] = str(body["model"]).strip()
            if "api_key" in body:  # 空字串 = 清除；未提供 = 保留原值
                CONFIG["api_key"] = str(body["api_key"]).strip()
            MODEL = None  # 清掉快取，強制重新偵測
            save_config()
            try:
                self._json({"ok": True, "model": get_model()})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
            return
        if self.path == "/api/reset":
            with LOCK:
                del HISTORY[1:]
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if self.path != "/api/chat":
            self.send_error(404)
            return
        self._sse_headers()

        def emit(ev):
            self.wfile.write(b"data: " + json.dumps(ev, ensure_ascii=False).encode("utf-8") + b"\n\n")
            self.wfile.flush()

        try:
            with LOCK:
                run_agent(str(body.get("message", "")).strip(), emit)
            emit({"type": "done"})
        except Exception as e:
            try:
                emit({"type": "error", "message": str(e)})
            except Exception:
                pass

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    handler = functools.partial(Handler, directory=here)
    server = http.server.ThreadingHTTPServer((HOST, PORT), handler)
    print(f"Voice agent: http://{HOST}:{PORT}  (os={OS_DESC}, LLM -> {CONFIG['upstream']}, "
          f"chrome={'yes' if CHROME else 'no'})")
    print("提醒: 麥克風需要安全來源。遠端存取請用 tailscale serve 或反向代理提供 HTTPS。")
    server.serve_forever()
