"""Tests for the deploy audit: ``pynext.check`` + ``pynext check --deploy``.

Every check function is exercised twice where it can fail: a safe
setting passes (no message, or an info note where the framework's
choice is deliberate), and a risky setting yields a warning or an
error with the right id. The CLI tests run ``cli.main()`` in-process
(the ``test_cli.py`` pattern) against a real ``pynext.config.py`` and
assert the audit never starts a server.
"""
import json
import sys
from dataclasses import replace

import pytest

from pynext import check as deploy_check
from pynext import cli
from pynext.check import (
    CHECKS,
    DeployReport,
    DeploySettings,
    run_deploy_checks,
)
from pynext.config import load_config


def ids(report, level=None):
    return [m.id for m in report.messages
            if level is None or m.level == level]


def test_check_registry_has_thirty_plus_checks():
    assert len(CHECKS) >= 30
    assert len({c.__name__ for c in CHECKS}) == len(CHECKS)


def test_default_settings_audit_has_no_errors():
    report = run_deploy_checks()
    assert report.ok
    assert report.errors == ()
    # Framework gaps the audit is meant to surface on a default deploy:
    # no Host allowlist (M-9) and no built-in HTTP->HTTPS redirect.
    assert "security.W020" in ids(report, "warning")
    assert "security.W008" in ids(report, "warning")
    # ... and the default deploy is not rate-limited or key-shared.
    assert "security.W117" in ids(report, "warning")
    assert "security.W025" in ids(report, "warning")


# --- debug / secret key ----------------------------------------------------

@pytest.mark.parametrize("debug,expected", [
    (False, []),
    (True, ["security.W018"]),
])
def test_check_debug(debug, expected):
    assert [m.id for m in deploy_check.check_debug(replace(DeploySettings(), debug=debug))] == expected
    assert deploy_check.check_debug(replace(DeploySettings(), debug=True))[0].level == "error"


@pytest.mark.parametrize("source,key_bytes,expected", [
    ("default", None, ["security.W025"]),
    ("env", 32, []),
    ("file", 32, []),
    ("cli", 32, []),
])
def test_check_secret_key(source, key_bytes, expected):
    settings = replace(DeploySettings(), csrf_signing_key_source=source,
                       csrf_signing_key_bytes=key_bytes)
    assert [m.id for m in deploy_check.check_secret_key(settings)] == expected


@pytest.mark.parametrize("key_bytes,expected", [
    (None, []),
    (16, ["security.W101"]),
    (31, ["security.W101"]),
    (32, []),
    (64, []),
])
def test_check_secret_key_strength(key_bytes, expected):
    settings = replace(DeploySettings(), csrf_signing_key_source="env",
                       csrf_signing_key_bytes=key_bytes)
    assert [m.id for m in deploy_check.check_secret_key_strength(settings)] == expected


@pytest.mark.parametrize("workers,source,expected", [
    (1, "default", []),
    (2, "default", ["security.W102"]),
    (4, "env", []),
    (4, "cli", []),
])
def test_check_csrf_key_shared(workers, source, expected):
    settings = replace(DeploySettings(), workers=workers, csrf_signing_key_source=source)
    assert [m.id for m in deploy_check.check_csrf_key_shared(settings)] == expected


def test_check_allowed_hosts_always_warns_until_s_p1_19():
    assert [m.id for m in deploy_check.check_allowed_hosts(DeploySettings())] == ["security.W020"]
    assert deploy_check.check_allowed_hosts(
        replace(DeploySettings(), allowed_hosts_configured=True)) == []


# --- HSTS / headers ----------------------------------------------------------

@pytest.mark.parametrize("kwargs,expected", [
    ({}, ["security.W103"]),  # preload off is only an info note
    ({"hsts_enabled": False}, ["security.W004", "security.W103"]),
    ({"hsts_max_age": 100}, ["security.W004", "security.W103"]),
    ({"hsts_max_age": 31_536_000}, ["security.W103"]),
    ({"hsts_include_subdomains": False}, ["security.W005", "security.W103"]),
    ({"hsts_preload": True}, []),
])
def test_check_hsts(kwargs, expected):
    assert [m.id for m in deploy_check.check_hsts(replace(DeploySettings(), **kwargs))] == expected


def test_check_ssl_redirect_warns():
    assert [m.id for m in deploy_check.check_ssl_redirect(DeploySettings())] == ["security.W008"]


@pytest.mark.parametrize("field,expected", [
    ("frame_options", ["security.W019"]),
    ("referrer_policy", ["security.W011"]),
    ("permissions_policy", ["security.W104"]),
])
def test_check_optional_headers(field, expected):
    assert [m.id for m in getattr(deploy_check, f"check_{field}")(
        replace(DeploySettings(), **{field: None}))] == expected
    # Defaults pass.
    assert getattr(deploy_check, f"check_{field}")(DeploySettings()) == []


@pytest.mark.parametrize("field,value,expected", [
    ("csp_frame_ancestors", "'none'", []),
    ("csp_frame_ancestors", "*", ["security.W105"]),
    ("csp_base_uri", "data:", ["security.W105"]),
    ("csp_form_action", "'self'", []),
])
def test_check_content_security_policy(field, value, expected):
    settings = replace(DeploySettings(), **{field: value})
    assert [m.id for m in deploy_check.check_content_security_policy(settings)] == expected


def test_check_effective_security_headers_passes_for_defaults():
    assert deploy_check.check_effective_security_headers(DeploySettings()) == []


def test_check_effective_security_headers_disabled_hsts_is_field_check_job():
    # HSTS off is the field check's finding (W004); the effective-headers
    # check only fires when HSTS is on but the header is still missing.
    settings = replace(DeploySettings(), hsts_enabled=False)
    assert deploy_check.check_effective_security_headers(settings) == []
    assert [m.id for m in deploy_check.check_hsts(settings)] == [
        "security.W004", "security.W103"]


def test_check_effective_security_headers_catches_missing_frame_options():
    # frame_options=None removes X-Frame-Options from the real header list;
    # the field check flags it and the effective check must agree there is
    # no framing header in the emitted set.
    settings = replace(DeploySettings(), frame_options=None)
    assert "security.W019" in [m.id for m in deploy_check.check_frame_options(settings)]


# --- cookies -----------------------------------------------------------------

@pytest.mark.parametrize("secure,expected", [
    (True, []),
    (False, ["security.W010"]),
])
def test_check_session_cookie_secure(secure, expected):
    settings = replace(DeploySettings(), session_cookie_secure=secure)
    assert [m.id for m in deploy_check.check_session_cookie_secure(settings)] == expected


@pytest.mark.parametrize("httponly,expected", [
    (True, []),
    (False, ["security.W013"]),
])
def test_check_session_cookie_httponly(httponly, expected):
    settings = replace(DeploySettings(), session_cookie_httponly=httponly)
    assert [m.id for m in deploy_check.check_session_cookie_httponly(settings)] == expected


@pytest.mark.parametrize("samesite,secure,expected,level", [
    ("Lax", True, [], None),
    ("Strict", True, [], None),
    ("None", True, ["security.W015"], "warning"),
    ("None", False, ["security.E107"], "error"),
])
def test_check_session_cookie_samesite(samesite, secure, expected, level):
    settings = replace(DeploySettings(), session_cookie_samesite=samesite,
                       session_cookie_secure=secure)
    found = deploy_check.check_session_cookie_samesite(settings)
    assert [m.id for m in found] == expected
    if level is not None:
        assert found[0].level == level


@pytest.mark.parametrize("max_age,expected_ids", [
    (86400, ["security.W109"]),  # info only: no absolute lifetime yet
    (14 * 24 * 3600, ["security.W109"]),
    (14 * 24 * 3600 + 1, ["security.W108", "security.W109"]),
])
def test_check_session_max_age(max_age, expected_ids):
    settings = replace(DeploySettings(), session_max_age=max_age)
    assert [m.id for m in deploy_check.check_session_max_age(settings)] == expected_ids


@pytest.mark.parametrize("secure,expected", [
    (True, []),
    (False, ["security.W016"]),
])
def test_check_csrf_cookie_secure(secure, expected):
    settings = replace(DeploySettings(), csrf_cookie_secure=secure)
    assert [m.id for m in deploy_check.check_csrf_cookie_secure(settings)] == expected


@pytest.mark.parametrize("samesite,secure,expected,level", [
    ("Lax", True, [], None),
    ("None", True, ["security.W110"], "warning"),
    ("None", False, ["security.E102"], "error"),
])
def test_check_csrf_cookie_samesite(samesite, secure, expected, level):
    settings = replace(DeploySettings(), csrf_cookie_samesite=samesite,
                       csrf_cookie_secure=secure)
    found = deploy_check.check_csrf_cookie_samesite(settings)
    assert [m.id for m in found] == expected
    if level is not None:
        assert found[0].level == level


def test_check_csrf_cookie_httponly_is_info_not_warning():
    found = deploy_check.check_csrf_cookie_httponly(DeploySettings())
    assert [m.id for m in found] == ["security.W111"]
    assert found[0].level == "info"


@pytest.mark.parametrize("enabled,expected,level", [
    (True, [], None),
    (False, ["security.W112"], "warning"),
    (None, ["security.W113"], "info"),
])
def test_check_csrf_enabled(enabled, expected, level):
    found = deploy_check.check_csrf_enabled(replace(DeploySettings(), csrf_enabled=enabled))
    assert [m.id for m in found] == expected
    if level is not None:
        assert found[0].level == level


def test_check_csrf_origin_check():
    assert deploy_check.check_csrf_origin_check(DeploySettings()) == []
    found = deploy_check.check_csrf_origin_check(
        replace(DeploySettings(), csrf_origin_check=False))
    assert [m.id for m in found] == ["security.W114"]


# --- CORS / rate limit / workers ----------------------------------------------

@pytest.mark.parametrize("origins,credentials,expected,level", [
    ((), False, [], None),
    (("https://example.com",), False, [], None),
    (("*",), False, ["security.W115"], "warning"),
    (("*",), True, ["security.E115"], "error"),
    (("https://example.com",), True, [], None),
])
def test_check_cors(origins, credentials, expected, level):
    settings = replace(DeploySettings(), cors_allowed_origins=origins,
                       cors_allow_credentials=credentials)
    found = deploy_check.check_cors(settings)
    assert [m.id for m in found] == expected
    if level is not None:
        assert found[0].level == level


def test_check_cors_origins_flags_plain_http():
    settings = replace(DeploySettings(), cors_allowed_origins=("http://example.com",))
    assert [m.id for m in deploy_check.check_cors_origins(settings)] == ["security.W116"]
    assert deploy_check.check_cors_origins(
        replace(DeploySettings(), cors_allowed_origins=("https://example.com",))) == []


@pytest.mark.parametrize("rate_limit,expected", [
    (0, ["security.W117"]),
    (100, []),
])
def test_check_rate_limit(rate_limit, expected):
    settings = replace(DeploySettings(), rate_limit=rate_limit)
    assert [m.id for m in deploy_check.check_rate_limit(settings)] == expected


def test_check_rate_limit_backend():
    assert deploy_check.check_rate_limit_backend(DeploySettings()) == []
    settings = replace(DeploySettings(), workers=4, rate_limit=100, rate_limit_backend="memory")
    assert [m.id for m in deploy_check.check_rate_limit_backend(settings)] == ["security.W118"]
    settings = replace(settings, rate_limit_backend="redis")
    assert deploy_check.check_rate_limit_backend(settings) == []


def test_check_workers_cache():
    assert deploy_check.check_workers_cache(DeploySettings()) == []
    settings = replace(DeploySettings(), workers=4, cache_backend="memory")
    assert [m.id for m in deploy_check.check_workers_cache(settings)] == ["security.W119"]
    settings = replace(settings, cache_backend="sqlite")
    assert deploy_check.check_workers_cache(settings) == []


# --- binding / websocket / proxy / misc ----------------------------------------

@pytest.mark.parametrize("host,debug,expected,level", [
    ("127.0.0.1", False, [], None),
    ("0.0.0.0", False, ["security.W120"], "warning"),
    ("0.0.0.0", True, ["security.E120"], "error"),
    ("127.0.0.1", True, [], None),
])
def test_check_host_binding(host, debug, expected, level):
    settings = replace(DeploySettings(), host=host, debug=debug)
    found = deploy_check.check_host_binding(settings)
    assert [m.id for m in found] == expected
    if level is not None:
        assert found[0].level == level


def test_check_websocket_origins():
    assert deploy_check.check_websocket_origins(DeploySettings()) == []
    settings = replace(DeploySettings(), websocket_allow_missing_origin=True)
    assert [m.id for m in deploy_check.check_websocket_origins(settings)] == ["security.W121"]
    settings = replace(DeploySettings(),
                       websocket_allowed_origins=("http://example.com",))
    assert [m.id for m in deploy_check.check_websocket_origins(settings)] == ["security.W122"]


@pytest.mark.parametrize("proxies,expected,level", [
    ((), [], None),
    (("10.0.0.1",), [], None),
    (("10.0.0.0/8",), [], None),
    (("0.0.0.0/0",), ["security.E123"], "error"),
    (("::/0",), ["security.E123"], "error"),
])
def test_check_trusted_proxies(proxies, expected, level):
    settings = replace(DeploySettings(), trusted_proxies=proxies)
    found = deploy_check.check_trusted_proxies(settings)
    assert [m.id for m in found] == expected
    if level is not None:
        assert found[0].level == level


def test_check_proxy_ssl_header_is_info():
    assert deploy_check.check_proxy_ssl_header(DeploySettings()) == []
    settings = replace(DeploySettings(), trusted_proxies=("10.0.0.1",))
    found = deploy_check.check_proxy_ssl_header(settings)
    assert [m.id for m in found] == ["security.W124"]
    assert found[0].level == "info"


def test_check_log_level():
    assert deploy_check.check_log_level(DeploySettings()) == []
    settings = replace(DeploySettings(), log_level="DEBUG")
    assert [m.id for m in deploy_check.check_log_level(settings)] == ["security.W125"]


def test_check_metrics_exposure():
    settings = replace(DeploySettings(), metrics=True, host="0.0.0.0")
    assert [m.id for m in deploy_check.check_metrics_exposure(settings)] == ["security.W126"]
    assert deploy_check.check_metrics_exposure(
        replace(DeploySettings(), metrics=True, host="127.0.0.1")) == []
    assert deploy_check.check_metrics_exposure(
        replace(DeploySettings(), metrics=None, host="0.0.0.0")) == []


@pytest.mark.parametrize("is_set,length,expected,level", [
    (False, 0, ["security.W128"], "info"),
    (True, 8, ["security.W127"], "warning"),
    (True, 16, [], None),
    (True, 40, [], None),
])
def test_check_revalidate_secret(is_set, length, expected, level):
    settings = replace(DeploySettings(), revalidate_secret_set=is_set,
                       revalidate_secret_length=length)
    found = deploy_check.check_revalidate_secret(settings)
    assert [m.id for m in found] == expected
    if level is not None:
        assert found[0].level == level


def test_check_cache_failure_policy_and_csp_nonce_are_info():
    found = deploy_check.check_cache_failure_policy(DeploySettings())
    assert [m.id for m in found] == ["security.W129"]
    assert found[0].level == "info"
    found = deploy_check.check_csp_nonce(DeploySettings())
    assert [m.id for m in found] == ["security.W130"]
    assert found[0].level == "info"
    assert deploy_check.check_cache_failure_policy(
        replace(DeploySettings(), cache_failure_policy="fail_closed")) == []
    assert deploy_check.check_csp_nonce(
        replace(DeploySettings(), csp_nonce_enabled=True)) == []


# --- report ---------------------------------------------------------------------

def test_report_counts_and_text_format():
    report = run_deploy_checks(replace(DeploySettings(), debug=True))
    assert not report.ok
    assert report.errors
    text = report.format_text()
    assert "security.W018" in text
    assert "System check identified" in text
    assert "Errors (" in text


def test_report_json_has_no_secret_value(tmp_path):
    # The key value itself must never appear in any report output, even
    # though the audit knows the key's source and byte length.
    secret = "z" * 32
    (tmp_path / "pynext.config.py").write_text(
        f"config = {{'csrf_signing_key': '{secret}'}}\n")
    resolved = load_config(tmp_path, "start", env={})
    settings = DeploySettings.from_resolved(resolved, env={})
    assert settings.csrf_signing_key_bytes == 32
    text = run_deploy_checks(settings).format_json()
    parsed = json.loads(text)
    assert parsed["ok"] is True
    assert parsed["errors"] == 0
    assert secret not in text
    assert "hex:" not in text


def test_report_empty_with_no_checks():
    # With no checks registered the report is empty, not an error.
    report = run_deploy_checks(DeploySettings(), checks=[])
    assert report.messages == ()
    assert report.ok
    assert "No issues found." in report.format_text()


# --- settings from a resolved config --------------------------------------------

def test_settings_from_resolved_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("PYNEXT_CSRF_SIGNING_KEY", raising=False)
    monkeypatch.delenv("PYNEXT_DEBUG", raising=False)
    monkeypatch.delenv("PYNEXT_REVALIDATE_SECRET", raising=False)
    resolved = load_config(tmp_path, "start", env={})
    settings = DeploySettings.from_resolved(resolved, env={})
    assert settings.csrf_signing_key_source == "default"
    assert settings.csrf_signing_key_bytes is None
    assert settings.csrf_cookie_secure is True  # production posture
    assert settings.debug is False
    assert settings.revalidate_secret_set is False


def test_settings_from_resolved_with_explicit_key(tmp_path, monkeypatch):
    key = "a" * 32
    resolved = load_config(tmp_path, "start", env={"PYNEXT_CSRF_SIGNING_KEY": key})
    settings = DeploySettings.from_resolved(
        resolved, env={"PYNEXT_CSRF_SIGNING_KEY": key})
    assert settings.csrf_signing_key_source == "env"
    assert settings.csrf_signing_key_bytes == 32
    assert run_deploy_checks(settings).errors == ()
    assert "security.W025" not in ids(run_deploy_checks(settings), "warning")


def test_settings_from_resolved_config_file(tmp_path, monkeypatch):
    (tmp_path / "pynext.config.py").write_text(
        "config = {'workers': 4, 'rate_limit': 100, 'csrf_signing_key': '"
        + "b" * 32 + "'}\n")
    monkeypatch.delenv("PYNEXT_CSRF_SIGNING_KEY", raising=False)
    resolved = load_config(tmp_path, "start", env={})
    settings = DeploySettings.from_resolved(resolved, env={})
    assert settings.workers == 4
    assert settings.rate_limit == 100
    assert settings.csrf_signing_key_source == "file"
    assert settings.csrf_signing_key_bytes == 32
    report = run_deploy_checks(settings)
    assert "security.W102" not in ids(report, "warning")  # key is shared
    assert "security.W119" in ids(report, "warning")  # memory cache + workers


def test_settings_from_resolved_debug_env(monkeypatch):
    resolved = load_config(".", "start", env={})
    settings = DeploySettings.from_resolved(resolved, env={"PYNEXT_DEBUG": "1"})
    assert settings.debug is True
    settings = DeploySettings.from_resolved(resolved, debug=False,
                                            env={"PYNEXT_DEBUG": "1"})
    assert settings.debug is False


# --- CLI ---------------------------------------------------------------------

def _run_cli(argv, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["pynext", *argv])
    return cli.main()


def test_cli_check_requires_deploy_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as error:
        _run_cli(["check", "--dir", str(tmp_path)], monkeypatch)
    assert error.value.code == 2
    assert "--deploy" in capsys.readouterr().err


def test_cli_check_deploy_exits_zero_on_warnings_only(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PYNEXT_CSRF_SIGNING_KEY", raising=False)
    code = _run_cli(["check", "--deploy", "--dir", str(tmp_path)], monkeypatch)
    assert code == 0
    out = capsys.readouterr().out
    assert "security.W020" in out
    assert "System check identified" in out


def test_cli_check_deploy_fails_on_errors(tmp_path, monkeypatch, capsys):
    (tmp_path / "pynext.config.py").write_text(
        "config = {'cors_allowed_origins': ['*'], 'cors_allow_credentials': True}\n")
    code = _run_cli(["check", "--deploy", "--dir", str(tmp_path)], monkeypatch)
    assert code == 1
    assert "security.E115" in capsys.readouterr().out


def test_cli_check_deploy_json(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PYNEXT_CSRF_SIGNING_KEY", raising=False)
    code = _run_cli(["check", "--deploy", "--format", "json", "--dir", str(tmp_path)],
                    monkeypatch)
    assert code == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["ok"] is True
    assert parsed["warnings"] > 0
    assert any(m["id"] == "security.W020" for m in parsed["messages"])


def test_cli_check_deploy_fail_on_warning(tmp_path, monkeypatch, capsys):
    code = _run_cli(["check", "--deploy", "--fail-on-warning", "--dir", str(tmp_path)],
                    monkeypatch)
    assert code == 1
    assert "security.W020" in capsys.readouterr().out


def test_cli_doctor_runs_same_audit(tmp_path, monkeypatch, capsys):
    code = _run_cli(["doctor", "--dir", str(tmp_path)], monkeypatch)
    assert code == 0
    out = capsys.readouterr().out
    assert "security.W020" in out
    assert "System check identified" in out


def test_cli_check_does_not_start_server(tmp_path, monkeypatch):
    # create_app / serve must never be called by the audit path.
    monkeypatch.setattr(cli, "create_app", lambda *a, **k: pytest.fail("unexpected startup"))
    monkeypatch.setattr(cli, "serve", lambda *a, **k: pytest.fail("unexpected startup"))
    code = _run_cli(["check", "--deploy", "--dir", str(tmp_path)], monkeypatch)
    assert code == 0


def test_cli_check_debug_flag_reports_error(tmp_path, monkeypatch, capsys):
    code = _run_cli(["check", "--deploy", "--debug", "--dir", str(tmp_path)], monkeypatch)
    assert code == 1
    assert "security.W018" in capsys.readouterr().out
