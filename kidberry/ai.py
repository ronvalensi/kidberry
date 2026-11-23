"""AI review providers: who reads the transcript text and returns corrections and flags.

Chosen in data/settings.json (environment variables override it, see config.py):

    "ai": {"provider": "claude-code", "model": "", "daily_token_cap": 150000, "mode": "smart"}

    claude-code  your Claude plan (Pro/Max) through the Claude Code CLI: no API key, no extra bill
    anthropic    a Claude API key, pay per use
    openai       an OpenAI API key, pay per use
    gemini       a Gemini API key from Google AI Studio; has a free tier (read its privacy note)
    none         no AI: keyword and sound flags only, nothing leaves this PC

"model" empty = a sensible default (for OpenAI and Gemini: the newest main model the key can use).

API keys go in data/secrets.json, which is never uploaded and never included in a support report:
    {"anthropic": "sk-ant-...", "openai": "sk-...", "gemini": "AIza..."}
or in the environment / .env: ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY.

Only transcript text is ever sent, never audio.

Check that a provider works and answers well (one small made-up batch, a few thousand tokens):
    python -m kidberry.ai --test
    python -m kidberry.ai --list          # the providers and their cost / privacy notes
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from . import config

SECRETS_PATH = config.DATA_DIR / "secrets.json"
CLAUDE_PRICE_IN, CLAUDE_PRICE_OUT = 4.0, 20.0   # USD per 1M tokens for claude-opus-5-5 (API list price)

PROVIDERS = {
    "claude-code": {
        "label": "Claude, on your Claude plan (via Claude Code)",
        "needs": "Claude Code installed and logged in to a Claude Pro or Max plan",
        "cost": "Uses your Claude plan's usage limits. No extra charge.",
        "privacy": "Claude's consumer terms apply. In claude.ai > Settings > Privacy you choose whether your chats "
                   "may be used to improve Claude; we suggest turning that off.",
        "workers": 2, "interval_s": 0},
    "anthropic": {
        "label": "Claude API key (pay per use)",
        "needs": "an API key from console.anthropic.com", "key": "anthropic", "env": "ANTHROPIC_API_KEY",
        "cost": "Billed per token to your Anthropic account: roughly $1 for a typical kindergarten day.",
        "privacy": "By Anthropic's API terms, what you send is not used for training by default.",
        "workers": 4, "interval_s": 0},
    "openai": {
        "label": "OpenAI API key (pay per use)",
        "needs": "an API key from platform.openai.com (a ChatGPT subscription does not include API use)",
        "key": "openai", "env": "OPENAI_API_KEY",
        "cost": "Billed per token to your OpenAI account.",
        "privacy": "By OpenAI's API terms, what you send is not used for training by default; it may be kept "
                   "for up to 30 days for abuse monitoring.",
        "workers": 3, "interval_s": 0},
    "gemini": {
        "label": "Google Gemini API key (free tier or paid)",
        "needs": "an API key from aistudio.google.com", "key": "gemini", "env": "GEMINI_API_KEY",
        "cost": "Free tier: no charge, but low daily limits, so a day may need a few runs to finish. "
                "With billing turned on: billed per token.",
        "privacy": "On the FREE tier, Google may use what you send to improve its products, and people may read "
                   "it. This is speech of children and staff: we recommend a paid key or another provider.",
        "workers": 1, "interval_s": 7},
    "none": {
        "label": "No AI",
        "needs": "nothing",
        "cost": "Free.",
        "privacy": "Nothing leaves this PC.",
        "workers": 0, "interval_s": 0},
}


class UsageLimitHit(Exception):
    """The plan's or key's usage limit / quota is reached: stop cleanly and continue on a later run."""


class ProviderError(RuntimeError):
    """The provider refused the request in a way retrying will not fix (bad key, bad model, ...)."""


def provider() -> str:
    return config.AI_BACKEND if config.AI_BACKEND in PROVIDERS else "claude-code"


def info(name: str | None = None) -> dict:
    name = name or provider()
    return {"provider": name, **{k: v for k, v in PROVIDERS[name].items() if k not in ("key", "env")},
            "has_key": bool(api_key(name)) if PROVIDERS[name].get("key") else None}


# ---------------------------------------------------------------- keys

def load_env() -> None:
    env = config.ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def api_key(name: str) -> str | None:
    p = PROVIDERS[name]
    if not p.get("key"):
        return None
    load_env()
    if os.environ.get(p["env"]):
        return os.environ[p["env"]]
    if name == "gemini" and os.environ.get("GOOGLE_API_KEY"):
        return os.environ["GOOGLE_API_KEY"]
    try:
        return json.loads(SECRETS_PATH.read_text(encoding="utf-8")).get(p["key"]) or None
    except (OSError, ValueError):
        return None


def _need_key(name: str) -> str:
    key = api_key(name)
    if not key:
        p = PROVIDERS[name]
        sys.exit(f"No API key for {p['label']}. Get {p['needs']}, then put it in {SECRETS_PATH} as\n"
                 f'  {{"{p["key"]}": "..."}}\nor set {p["env"]}.')
    return key


# ---------------------------------------------------------------- HTTP with retries

class _Pace:
    """At most one request every interval_s seconds (free tiers allow only a few requests a minute)."""
    def __init__(self, interval_s: float):
        self.interval, self.next, self.lock = interval_s, 0.0, threading.Lock()

    def wait(self) -> None:
        if not self.interval:
            return
        with self.lock:
            delay = self.next - time.time()
            if delay > 0:
                time.sleep(delay)
            self.next = time.time() + self.interval


def _http(method: str, url: str, headers: dict, body: dict | None = None, timeout: int = 600,
          quota_words: tuple = (), tries: int = 6) -> dict:
    """JSON request. Retries rate limits (429) and server errors with backoff, honouring Retry-After.
    A 429 that is a daily quota or out-of-credit (quota_words in the reply) stops at once: UsageLimitHit."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if e.code in (401, 403):
                raise ProviderError(f"the API key was refused ({e.code}). Check the key. {_short(text)}")
            if e.code == 429 and any(w in text for w in quota_words):
                raise UsageLimitHit(f"usage limit reached: {_short(text)}")
            if (e.code == 429 or e.code >= 500) and attempt < tries - 1:
                wait = _retry_after(e, text) or min(90, 5 * 2 ** attempt)
                print(f"  (provider busy, {e.code}; waiting {wait:.0f} s and retrying)")
                time.sleep(wait)
                continue
            if e.code == 429:
                raise UsageLimitHit(f"rate limit still hit after retries: {_short(text)}")
            raise ProviderError(f"HTTP {e.code}: {_short(text)}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < tries - 1:
                time.sleep(min(60, 5 * 2 ** attempt))
                continue
            raise ProviderError(f"could not reach the provider: {e}")
    raise ProviderError("no answer")


def _retry_after(e, text: str) -> float:
    try:
        header = float(e.headers.get("Retry-After") or 0)
        if header > 0:
            return min(120.0, header)
    except ValueError:
        pass
    m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', text)    # Gemini puts it in the body
    return min(120.0, float(m.group(1))) if m else 0.0


def _short(text: str) -> str:
    try:
        err = json.loads(text).get("error")
        text = err.get("message", text) if isinstance(err, dict) else str(err or text)
    except (ValueError, AttributeError):
        pass
    return " ".join(text.split())[:300]


def _parse_json(text: str):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        return None


# ---------------------------------------------------------------- providers
# Each review(prompt) returns (result_dict_or_None, tokens_in, tokens_out, cost_usd).
# None = the model declined or the answer was cut off; that batch is skipped.

class ClaudeCode:
    """Each batch through `claude -p` on the logged-in Claude plan. Kept lean: our own short system
    prompt replaces Claude Code's; no tools, MCP servers, CLAUDE.md, plugins or saved session."""
    name = "claude-code"
    workers = 2

    def __init__(self, system: str, schema: dict):
        self.exe = shutil.which("claude")
        if not self.exe:
            sys.exit("Claude Code ('claude') was not found. Install Claude Code and log in with your Claude plan, "
                     "or choose another AI provider.")
        self.model = config.AI_MODEL or "claude-opus-5-5"
        self.schema = schema
        fd, self.system_file = tempfile.mkstemp(prefix="kidberry_system_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(system)
        self.env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # force the plan login

    def review(self, prompt: str):
        cmd = [self.exe, "-p", "--output-format", "json", "--system-prompt-file", self.system_file,
               "--tools", "", "--strict-mcp-config", "--safe-mode", "--no-session-persistence",
               "--model", self.model, "--effort", config.AI_EFFORT, "--json-schema", json.dumps(self.schema)]
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                              env=self.env, timeout=900, cwd=tempfile.gettempdir())
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-500:]}")
        u = data.get("usage") or {}
        t_in = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
        t_out = u.get("output_tokens", 0)
        cost = data.get("total_cost_usd") or 0.0
        if data.get("is_error"):
            msg = str(data.get("result") or data.get("api_error_status") or "unknown error")
            if "limit" in msg.lower():
                raise UsageLimitHit(msg)
            raise RuntimeError(msg)
        if data.get("stop_reason") == "refusal":
            return None, t_in, t_out, cost
        result = data.get("structured_output")
        if result is None:
            result = _parse_json(data.get("result"))
        return result, t_in, t_out, cost


class Anthropic:
    """Claude API key, pay per use."""
    name = "anthropic"
    workers = 4

    def __init__(self, system: str, schema: dict):
        os.environ["ANTHROPIC_API_KEY"] = _need_key("anthropic")
        import anthropic
        self.client = anthropic.Anthropic()
        self.model = config.AI_MODEL or "claude-opus-5-5"
        self.system, self.schema = system, schema

    def review(self, prompt: str):
        import anthropic
        try:
            resp = self.client.beta.messages.create(
                model=self.model, max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                system=self.system,
                output_config={"effort": config.AI_EFFORT, "format": {"type": "json_schema", "schema": self.schema}},
                messages=[{"role": "user", "content": prompt}])
        except anthropic.AuthenticationError as e:
            raise ProviderError(f"the API key was refused. Check the key. {e}")
        except anthropic.RateLimitError as e:
            raise UsageLimitHit(str(e)[:300])
        t_in, t_out = resp.usage.input_tokens, resp.usage.output_tokens
        cost = (t_in * CLAUDE_PRICE_IN + t_out * CLAUDE_PRICE_OUT) / 1e6
        if resp.stop_reason in ("refusal", "max_tokens"):
            return None, t_in, t_out, cost
        return _parse_json(next((b.text for b in resp.content if b.type == "text"), "")), t_in, t_out, cost


def _newest(names: list[str], pattern: str) -> str | None:
    """The highest-numbered name matching pattern (group 1 = the version number, e.g. '2.5')."""
    best = None
    for n in names:
        m = re.fullmatch(pattern, n)
        if m:
            v = tuple(int(x) for x in m.group(1).split("."))
            if best is None or v > best[0]:
                best = (v, n)
    return best[1] if best else None


class OpenAI:
    """OpenAI API key (Responses API with a strict JSON schema), pay per use."""
    name = "openai"
    workers = 3
    URL = "https://api.openai.com/v1"

    def __init__(self, system: str, schema: dict):
        self.headers = {"Authorization": f"Bearer {_need_key('openai')}"}
        self.system, self.schema = system, schema
        self.model = config.AI_MODEL or self._pick_model()

    def _pick_model(self) -> str:
        ids = [m["id"] for m in _http("GET", f"{self.URL}/models", self.headers, timeout=60).get("data", [])]
        model = _newest(ids, r"gpt-(\d+(?:\.\d+)?)")   # the main model, not -mini/-nano/dated snapshots
        if not model:
            raise ProviderError('could not choose a model; set "model" under "ai" in settings.json')
        return model

    def review(self, prompt: str):
        body = {"model": self.model, "instructions": self.system, "input": prompt, "store": False,
                "text": {"format": {"type": "json_schema", "name": "review", "schema": self.schema, "strict": True}}}
        data = _http("POST", f"{self.URL}/responses", self.headers, body,
                     quota_words=("insufficient_quota",))   # out of credit
        u = data.get("usage") or {}
        t_in, t_out = u.get("input_tokens", 0), u.get("output_tokens", 0)
        if data.get("status") == "incomplete":
            return None, t_in, t_out, 0.0
        text = ""
        for item in data.get("output", []):
            for c in item.get("content") or []:
                if c.get("type") == "refusal":
                    return None, t_in, t_out, 0.0
                if c.get("type") == "output_text":
                    text += c.get("text", "")
        return _parse_json(text), t_in, t_out, 0.0


class Gemini:
    """Gemini API key from Google AI Studio (free tier or paid)."""
    name = "gemini"
    workers = 1
    URL = "https://generativelanguage.googleapis.com/v1beta"
    # The review is about possible harm to children: the default filters would block exactly that.
    SAFETY = [{"category": c, "threshold": "BLOCK_NONE"} for c in
              ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_SEXUALLY_EXPLICIT",
               "HARM_CATEGORY_DANGEROUS_CONTENT")]

    def __init__(self, system: str, schema: dict):
        self.headers = {"x-goog-api-key": _need_key("gemini")}
        self.system, self.schema = system, schema
        self.schema_ok = True
        self.pace = _Pace(float(config.AI_INTERVAL_S if config.AI_INTERVAL_S is not None
                                else PROVIDERS["gemini"]["interval_s"]))
        self.workers = config.AI_WORKERS or self.workers
        self.model = config.AI_MODEL or self._pick_model()

    def _pick_model(self) -> str:
        data = _http("GET", f"{self.URL}/models?pageSize=1000", self.headers, timeout=60)
        names = [m["name"].removeprefix("models/") for m in data.get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
        # The newest stable Flash: good with Hebrew, and the free tier allows far more of it than of Pro.
        model = _newest(names, r"gemini-(\d+(?:\.\d+)?)-flash") or _newest(names, r"gemini-(\d+(?:\.\d+)?)-pro")
        if not model:
            raise ProviderError('could not choose a model; set "model" under "ai" in settings.json')
        return model

    def review(self, prompt: str):
        system = self.system if self.schema_ok else (
            self.system + "\n\nAnswer with JSON only, matching this JSON schema:\n" + json.dumps(self.schema))
        config_ = {"responseMimeType": "application/json"}
        if self.schema_ok:
            config_["responseJsonSchema"] = self.schema
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": config_, "safetySettings": self.SAFETY}
        self.pace.wait()
        try:
            data = _http("POST", f"{self.URL}/models/{self.model}:generateContent", self.headers, body,
                         quota_words=("PerDay",))   # a daily quota; per-minute limits are waited out
        except ProviderError as e:
            if self.schema_ok and "HTTP 400" in str(e) and "schema" in str(e).lower():
                self.schema_ok = False          # an older model without full JSON-schema support
                return self.review(prompt)
            raise
        u = data.get("usageMetadata") or {}
        t_in = u.get("promptTokenCount", 0)
        t_out = u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)
        cands = data.get("candidates") or []
        if not cands or cands[0].get("finishReason") not in (None, "STOP"):
            return None, t_in, t_out, 0.0
        text = "".join(p.get("text", "") for p in (cands[0].get("content") or {}).get("parts", []) if not p.get("thought"))
        return _parse_json(text), t_in, t_out, 0.0


CLASSES = {"claude-code": ClaudeCode, "anthropic": Anthropic, "openai": OpenAI, "gemini": Gemini}


def make_reviewer(system: str, schema: dict):
    """The reviewer for the chosen provider, or None for "none"."""
    name = provider()
    if name == "none":
        return None
    return CLASSES[name](system, schema)


# ---------------------------------------------------------------- python -m kidberry.ai --test

def _test_lines():
    """A small made-up batch from this family's settings: a misheard name to correct, the everyday word
    that sounds like it to leave alone, a threat to flag and ordinary lines not to flag."""
    from . import settings
    child = settings.S["child"]
    name = child["name"] or "נועם"
    heard = (child["heard_as"] or [None])[0]
    common = (child["heard_as_common_word"] or [None])[0]
    texts = []
    if heard:
        texts.append(("name", f"{heard}, בוא תשב פה ליד השולחן"))
    if common:
        texts.append(("common", f"{common} מים, רק מיץ היום"))
    texts += [("threat", "אם לא תשתוק עכשיו אני ארביץ לך"), ("calm", "יאללה ילדים, הולכים לחצר"),
              ("calm", "מי רוצה עוד פירות?")]
    rows = [{"id": i + 1, "start": 600 + 9 * i, "end": 606 + 9 * i, "language": "he", "text": t}
            for i, (_, t) in enumerate(texts)]
    return name, common, [k for k, _ in texts], rows


def test() -> int:
    from .analyze import SCHEMA, SYSTEM, build_prompt
    name = provider()
    print(f"Provider: {PROVIDERS[name]['label']}")
    if name == "none":
        print("No AI is chosen: nothing to test.")
        return 0
    reviewer = make_reviewer(SYSTEM, SCHEMA)
    print(f"Model: {reviewer.model}")
    child, common, kinds, rows = _test_lines()
    prompt = build_prompt("test.wav", [], rows)
    t0 = time.time()
    try:
        result, t_in, t_out, cost = reviewer.review(prompt)
    except (UsageLimitHit, ProviderError, RuntimeError) as e:
        print(f"FAILED: {e}")
        return 1
    print(f"Answered in {time.time() - t0:.0f} s, {t_in:,} + {t_out:,} tokens")
    if not isinstance(result, dict) or not isinstance(result.get("lines"), list):
        print("FAILED: the answer was not the expected JSON")
        return 1
    got = {l.get("id"): l.get("corrected", "") for l in result["lines"] if isinstance(l, dict)}
    flagged = {i for f in result.get("flags", []) for i in f.get("line_ids", [])}
    severity = {i: f.get("severity", 0) for f in result.get("flags", []) for i in f.get("line_ids", [])}
    checks = [("all lines returned", all(r["id"] in got for r in rows))]
    for r, kind in zip(rows, kinds):
        if kind == "name":
            checks.append((f"misheard name corrected to {child}", child in got.get(r["id"], "")))
        elif kind == "common":
            checks.append((f"everyday word '{common}' left alone", common in got.get(r["id"], "")
                           and child not in got.get(r["id"], "")))
        elif kind == "threat":
            checks.append(("threat flagged (severity 3+)", r["id"] in flagged and severity.get(r["id"], 0) >= 3))
        else:
            checks.append((f"ordinary line #{r['id']} not flagged", r["id"] not in flagged))
    for label, ok in checks:
        print(f"  {'ok  ' if ok else 'MISS'} {label}")
    passed = sum(ok for _, ok in checks)
    print(f"{passed} of {len(checks)} checks passed." + ("" if passed == len(checks) else
          " A miss is not always a problem (the AI may phrase things differently), but several misses mean "
          "this provider or model will give weaker results."))
    return 0 if passed >= len(checks) - 1 else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true", help="send one small made-up batch and check the answer")
    ap.add_argument("--list", action="store_true", help="show the providers")
    args = ap.parse_args()
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")   # Hebrew in a piped/log output on Windows
    if args.test:
        sys.exit(test())
    current = provider()
    for key, p in PROVIDERS.items():
        mark = "*" if key == current else " "
        key_note = ""
        if p.get("key"):
            key_note = " (key found)" if api_key(key) else " (no key yet)"
        print(f"{mark} {key:<12} {p['label']}{key_note}\n    needs:   {p['needs']}\n    cost:    {p['cost']}\n"
              f"    privacy: {p['privacy']}")
    print('\nChoose with "ai": {"provider": "..."} in data/settings.json.')


if __name__ == "__main__":
    main()
