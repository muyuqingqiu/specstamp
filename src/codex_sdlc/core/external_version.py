from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.structured_contract import canonical_json_bytes, sha256_bytes, validate_schema_document


EXTERNAL_VERSION_SCHEMA = "external-version-evidence.v1"
SENSITIVE_QUERY_NAMES = {
    "access_key",
    "access_token",
    "api_key",
    "auth",
    "auth_token",
    "authorization",
    "bearer_token",
    "client_secret",
    "credential",
    "id_token",
    "password",
    "passwd",
    "private_key",
    "refresh_token",
    "secret",
    "secret_key",
    "session_token",
    "signature",
    "sig",
    "token",
    "x_api_key",
}
SENSITIVE_QUERY_SUFFIXES = (
    "access_key",
    "access_token",
    "api_key",
    "auth_token",
    "authorization",
    "client_secret",
    "credential",
    "id_token",
    "password",
    "passwd",
    "private_key",
    "refresh_token",
    "secret_key",
    "session_token",
    "signature",
)


def _decode_percent_layers(value: str) -> str:
    """最多解码四层，覆盖双重编码绕过，同时避免异常输入造成无限循环。"""

    current = str(value or "")
    for _ in range(4):
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    return current


def _normalize_url_field_name(value: str) -> str:
    decoded = _decode_percent_layers(value).strip()
    decoded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", decoded)
    decoded = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", decoded)
    return re.sub(r"[^a-z0-9]+", "_", decoded.lower()).strip("_")


def _is_sensitive_url_field(value: str) -> bool:
    normalized = _normalize_url_field_name(value)
    return normalized in SENSITIVE_QUERY_NAMES or any(
        normalized.endswith(f"_{suffix}") for suffix in SENSITIVE_QUERY_SUFFIXES
    )


def _fragment_contains_sensitive_field(fragment: str) -> bool:
    decoded = _decode_percent_layers(fragment)
    # 片段可能是 query 形式，也可能嵌在前端路由后；只检查明确的字段赋值，不误判普通文字。
    for part in re.split(r"[&;?]", decoded):
        candidate = part.rsplit("/", 1)[-1].strip()
        match = re.match(r"([^=:]+)\s*[=:]", candidate)
        if match and _is_sensitive_url_field(match.group(1)):
            return True
    return False


def normalize_external_url(raw_url: str) -> str:
    """只规范地址本身，不联网，也不读取远端页面正文。"""

    clean = str(raw_url or "").strip()
    try:
        parsed = urlsplit(clean)
    except ValueError as exc:
        raise SdlcError("外部资料 URL 格式无效。", exit_code=1) from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise SdlcError("外部资料 URL 只支持完整的 http 或 https 地址。", exit_code=1)
    if parsed.username or parsed.password:
        raise SdlcError("外部资料 URL 不能包含账号或密码。", exit_code=1)

    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    if any(_is_sensitive_url_field(name) for name, _ in query_items):
        # 错误里不回显地址，避免带密参数再次进入标准错误。
        raise SdlcError("外部资料 URL 不能包含密码、令牌或签名参数。", exit_code=1)
    if _fragment_contains_sensitive_field(parsed.fragment):
        raise SdlcError("外部资料 URL 片段不能包含密码、令牌或签名参数。", exit_code=1)

    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    port = parsed.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = urlencode(sorted(query_items), doseq=True)
    return urlunsplit((scheme, host, path, query, ""))


def normalized_url_sha256(raw_url: str) -> str:
    return sha256_bytes(normalize_external_url(raw_url).encode("utf-8"))


def unversioned_evidence(raw_url: str) -> dict[str, Any]:
    document = {
        "schema_version": EXTERNAL_VERSION_SCHEMA,
        "normalized_url_sha256": normalized_url_sha256(raw_url),
        "status": "unversioned",
        "evidence": None,
    }
    validate_schema_document(document, schema_name=EXTERNAL_VERSION_SCHEMA)
    return document


def load_external_version_evidence(path: Path, *, raw_url: str) -> dict[str, Any]:
    """证据必须来自结构化文件，不能把远端正文当成版本号。"""

    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError("外部版本证据文件读取失败或不是有效 JSON。", exit_code=1) from exc
    validate_schema_document(document, schema_name=EXTERNAL_VERSION_SCHEMA)
    if document["normalized_url_sha256"] != normalized_url_sha256(raw_url):
        raise SdlcError("外部版本证据绑定的 URL 与当前资料不一致。", exit_code=1)
    if document["status"] != "confirmed":
        raise SdlcError("外部版本证据没有确认稳定版本，资料保持阻塞。", exit_code=1)
    return deepcopy(document)


def compare_external_version_evidence(expected: object, observed: object) -> dict[str, Any]:
    """只比较明确版本字段；任何差异都按漂移处理，不解析正文含义。"""

    validate_schema_document(expected, schema_name=EXTERNAL_VERSION_SCHEMA)
    validate_schema_document(observed, schema_name=EXTERNAL_VERSION_SCHEMA)
    expected_document = deepcopy(expected)
    observed_document = deepcopy(observed)
    same_url = expected_document["normalized_url_sha256"] == observed_document["normalized_url_sha256"]
    same_evidence = canonical_json_bytes(expected_document.get("evidence")) == canonical_json_bytes(
        observed_document.get("evidence")
    )
    confirmed = expected_document.get("status") == "confirmed" and observed_document.get("status") == "confirmed"
    return {
        "status": "confirmed" if same_url and same_evidence and confirmed else "drifted",
        "same_url": same_url,
        "same_evidence": same_evidence,
    }


__all__ = [
    "EXTERNAL_VERSION_SCHEMA",
    "compare_external_version_evidence",
    "load_external_version_evidence",
    "normalize_external_url",
    "normalized_url_sha256",
    "unversioned_evidence",
]
