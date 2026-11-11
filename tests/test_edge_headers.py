"""The smoke test's edge-header case, run offline against its EXACT shipped bodies (#437).

`deploy/smoke-test.sh` case 10 GETs `/up` through the edge over HTTP/1.1 and HTTP/2 and
asserts every header the INSTALLED Caddyfile sets arrives once, with its value. Three
properties are pinned here, because each is one a live run can only ever pass or fail
without saying why:

* **The reader misses nothing.** Every `header` line in every host's Caddyfile in this
  tree is one the shipped reader extracts, checked against an independent parse, so a
  header the edge sets is a header the gate asserts.
* **The floor holds under the old Caddyfile and the new one.** Every floor name is set by
  every host's Caddyfile here. Reading the installed file is what keeps the
  gate passing under both (`deploy/README.md`), and the floor is what keeps an installed
  file that stopped setting a header from passing vacuously.
* **The gate discriminates.** A fake `curl` answers with each failure shape (absent,
  wrong value, sent twice, the wrong protocol, no answer) and each must FAIL, while the
  full set must PASS: a gate that always passes is as useless as one that always fails.

No network is touched: `curl` is a fake on PATH that writes a header dump.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SMOKE_TEST = ROOT / "deploy" / "smoke-test.sh"
CADDYFILES = sorted((ROOT / "deploy" / "hosts").glob("*/Caddyfile"))

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is required to run the shipped smoke-test bodies"
)


def _body() -> str:
    text = SMOKE_TEST.read_text()
    match = re.search(
        r"[ \t]*# >>> edge_headers >>>\n(.*?)[ \t]*# <<< edge_headers <<<\n", text, re.DOTALL
    )
    assert match, "edge_headers marker block not found in deploy/smoke-test.sh"
    return match.group(1)


def _run(snippet: str, tmp_path: Path, *, path: str | None = None) -> subprocess.CompletedProcess:
    """Run ``snippet`` after the shipped body, under the script's own shell options."""
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    script = tmp_path / "run.sh"
    script.write_text(
        "set -euo pipefail\n"
        'green() { printf "%s\\n" "$*"; }\n'
        'red() { printf "%s\\n" "$*"; }\n'
        "rc=0\n"
        f"workdir={workdir}\n" + _body() + snippet
    )
    env = {"PATH": f"{path}:/usr/bin:/bin" if path else "/usr/bin:/bin", "HOME": str(tmp_path)}
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)


def _read_site(caddyfile: Path, site: str, tmp_path: Path) -> subprocess.CompletedProcess:
    return _run(
        textwrap.dedent(
            f"""\
            if caddy_site_headers {caddyfile} {site}; then
                for ((i = 0; i < ${{#SITE_HEADER_NAMES[@]}}; i++)); do
                    printf '%s\\t%s\\n' "${{SITE_HEADER_NAMES[i]}}" "${{SITE_HEADER_VALUES[i]}}"
                done
            else
                printf 'REFUSED %s\\n' "$SITE_HEADER_ERROR"
                exit 3
            fi
            """
        ),
        tmp_path,
    )


def _independent_parse(caddyfile: Path) -> list[tuple[str, str]]:
    """Every `header` directive in the file, by a regex that shares nothing with the reader."""
    return re.findall(r'(?m)^\s*header\s+(\S+)\s+"([^"]*)"\s*$', caddyfile.read_text())


# --- the reader against this tree's Caddyfiles -------------------------------------


def test_the_glob_found_every_hosts_caddyfile() -> None:
    assert len(CADDYFILES) >= 3


@pytest.mark.parametrize("caddyfile", CADDYFILES, ids=lambda p: p.parent.name)
def test_every_header_a_hosts_caddyfile_sets_is_one_the_gate_asserts(
    caddyfile: Path, tmp_path: Path
) -> None:
    result = _read_site(caddyfile, caddyfile.parent.name, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    read = [tuple(line.split("\t", 1)) for line in result.stdout.splitlines()]
    assert read == _independent_parse(caddyfile)
    assert read, "a site with no headers would make the gate assert nothing"


def _floor(tmp_path: Path) -> list[str]:
    return _run('printf "%s\\n" "${EDGE_FLOOR_HEADERS[@]}"\n', tmp_path).stdout.split()


@pytest.mark.parametrize("caddyfile", CADDYFILES, ids=lambda p: p.parent.name)
def test_the_floor_is_set_by_every_hosts_caddyfile(caddyfile: Path, tmp_path: Path) -> None:
    """A floor header dropped from the tree must leave the floor in the same commit.

    The other direction (the floor growing) cannot be pinned here: it waits until every
    box's INSTALLED Caddyfile sets the name, which only the boxes know (smoke-test.sh).
    `tests/test_caddyfile.py` pins the floor to the set it pins, so there is one list.
    """
    floor = _floor(tmp_path)
    names = {name for name, _ in _independent_parse(caddyfile)}
    assert floor and set(floor) <= names


# --- the reader refuses what it cannot read, never guesses -------------------------

SITE = "nova.example.com"


def _caddyfile(tmp_path: Path, site_body: str, *, extra: str = "") -> Path:
    path = tmp_path / "Caddyfile"
    path.write_text(
        "{\n\tservers {\n\t\tstrict_sni_host on\n\t}\n}\n\n"
        f"{SITE} {{\n{textwrap.indent(textwrap.dedent(site_body), chr(9))}}}\n\n"
        "http:// {\n\trespond 421\n}\n" + extra
    )
    return path


@pytest.mark.parametrize(
    ("line", "why"),
    [
        ('header @api X-Frame-Options "DENY"', "a matcher"),
        ("header -Server", "a delete"),
        ('header +X-Frame-Options "DENY"', "an append"),
        ("header X-Frame-Options DENY", "an unquoted value"),
        ('header X-Frame-Options "DENY" # legacy', "a trailing comment"),
        ("header {\n\tX-Frame-Options DENY\n}", "a block"),
        ('handle /up {\n\theader X-Frame-Options "DENY"\n}', "a nested directive"),
        ('header X-Frame-Options "DENY"\nheader x-frame-options "SAMEORIGIN"', "a second set"),
        ('header X-Served-By "{system.hostname}"', "a placeholder Caddy substitutes"),
        ('header X-Frame-Options "DENY "', "a trailing space HTTP would trim"),
        ('header X-Frame-Options " DENY"', "a leading space HTTP would trim"),
        ('header X-Frame-Options ""', "an empty value"),
    ],
)
def test_a_header_line_the_reader_cannot_read_is_refused_by_line(
    line: str, why: str, tmp_path: Path
) -> None:
    caddyfile = _caddyfile(tmp_path, f'header Referrer-Policy "no-referrer"\n{line}\n')
    result = _read_site(caddyfile, SITE, tmp_path)
    assert result.returncode == 3, why
    assert f"REFUSED {caddyfile}:" in result.stdout, why


def test_a_missing_site_block_is_refused(tmp_path: Path) -> None:
    caddyfile = _caddyfile(tmp_path, 'header X-Frame-Options "DENY"\n')
    result = _read_site(caddyfile, "john.example.com", tmp_path)
    assert result.returncode == 3
    assert "no site block `john.example.com {`" in result.stdout


def test_an_unreadable_caddyfile_is_refused(tmp_path: Path) -> None:
    result = _read_site(tmp_path / "absent", SITE, tmp_path)
    assert result.returncode == 3
    assert "cannot read the installed Caddyfile" in result.stdout


def test_only_the_hosts_own_site_is_read(tmp_path: Path) -> None:
    """A header in another site never becomes an expectation of this one's answer."""
    other = 'john.example.com {\n\theader X-Frame-Options "SAMEORIGIN"\n}\n'
    caddyfile = _caddyfile(
        tmp_path, 'request_body {\n\tmax_size 25MB\n}\nheader X-Frame-Options "DENY"\n', extra=other
    )
    result = _read_site(caddyfile, SITE, tmp_path)
    assert result.returncode == 0, result.stdout
    assert result.stdout.splitlines() == ["X-Frame-Options\tDENY"]


def test_an_installed_file_without_the_floor_fails_never_passes_vacuously(tmp_path: Path) -> None:
    caddyfile = _caddyfile(tmp_path, 'header X-Frame-Options "DENY"\n')
    result = _run(
        f"caddy_site_headers {caddyfile} {SITE}\nassert_edge_floor\necho rc=$rc\n", tmp_path
    )
    assert result.stdout.count("  FAIL  ") == 5
    assert "  PASS  edge header X-Frame-Options is set" in result.stdout
    assert result.stdout.endswith("rc=1\n")


# --- the live assertion against a fake edge ----------------------------------------

#: What the edge answers today, over HTTP/1.1 (canonical case) and HTTP/2 (lower case).
GOOD_H1 = (
    "HTTP/1.1 200 OK\r\n"
    "Content-Type: application/json\r\n"
    "X-Frame-Options: DENY\r\n"
    "Referrer-Policy: no-referrer\r\n"
    "\r\n"
)
GOOD_H2 = "HTTP/2 200 \r\nx-frame-options: DENY\r\nreferrer-policy:  no-referrer \r\n\r\n"


def _fake_curl(tmp_path: Path, dump: str, *, exit_code: int = 0) -> Path:
    """A `curl` that writes ``dump`` to its `-D` file and logs its argv, never the network."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (tmp_path / "dump.txt").write_text(dump)
    fake = bindir / "curl"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >>"{tmp_path}/argv.log"\n'
        'while (($#)); do if [[ "$1" == -D ]]; then '
        f'cp "{tmp_path}/dump.txt" "$2"; fi; shift; done\n'
        f"[[ {exit_code} -eq 0 ]] || echo 'curl: (7) Failed to connect' >&2\n"
        f"exit {exit_code}\n"
    )
    fake.chmod(0o755)
    return bindir


def _assert_edge(
    tmp_path: Path, dump: str, flag: str, proto: str, *, exit_code: int = 0
) -> subprocess.CompletedProcess:
    caddyfile = _caddyfile(
        tmp_path, 'header X-Frame-Options "DENY"\nheader Referrer-Policy "no-referrer"\n'
    )
    bindir = _fake_curl(tmp_path, dump, exit_code=exit_code)
    return _run(
        f"caddy_site_headers {caddyfile} {SITE}\n"
        f"assert_edge_headers https://{SITE}/up {flag} {proto}\n"
        "echo rc=$rc\n",
        tmp_path,
        path=str(bindir),
    )


@pytest.mark.parametrize(
    ("dump", "flag", "proto"),
    [(GOOD_H1, "--http1.1", "HTTP/1.1"), (GOOD_H2, "--http2", "HTTP/2")],
)
def test_the_full_set_passes_one_line_per_header(
    dump: str, flag: str, proto: str, tmp_path: Path
) -> None:
    result = _assert_edge(tmp_path, dump, flag, proto)
    assert result.stdout.splitlines() == [
        f"  PASS  edge header X-Frame-Options over {proto}: DENY",
        f"  PASS  edge header Referrer-Policy over {proto}: no-referrer",
        "rc=0",
    ], result.stderr
    argv = (tmp_path / "argv.log").read_text()
    assert flag in argv and f"https://{SITE}/up" in argv


@pytest.mark.parametrize(
    ("dump", "needle"),
    [
        (GOOD_H1.replace("X-Frame-Options: DENY\r\n", ""), "X-Frame-Options over HTTP/1.1: absent"),
        (
            GOOD_H1.replace(": DENY", ": SAMEORIGIN"),
            "X-Frame-Options over HTTP/1.1: SAMEORIGIN (EXPECTED DENY)",
        ),
        (GOOD_H1.replace("\r\n\r\n", "\r\nx-frame-options: DENY\r\n\r\n"), "sent 2 times"),
        (
            GOOD_H1.replace("HTTP/1.1 200 OK", "HTTP/2 200"),
            "answered over HTTP/2 (EXPECTED HTTP/1.1)",
        ),
    ],
    ids=["absent", "wrong-value", "sent-twice", "wrong-protocol"],
)
def test_each_failure_shape_fails(dump: str, needle: str, tmp_path: Path) -> None:
    result = _assert_edge(tmp_path, dump, "--http1.1", "HTTP/1.1")
    assert "  FAIL  " in result.stdout and needle in result.stdout, result.stdout
    assert result.stdout.endswith("rc=1\n")


def test_a_header_after_the_header_block_is_not_read(tmp_path: Path) -> None:
    """Only the response's own headers count, never a line that follows the blank line."""
    dump = GOOD_H1.replace("X-Frame-Options: DENY\r\n", "") + "X-Frame-Options: DENY\r\n"
    result = _assert_edge(tmp_path, dump, "--http1.1", "HTTP/1.1")
    assert "X-Frame-Options over HTTP/1.1: absent" in result.stdout


def test_no_answer_at_all_fails(tmp_path: Path) -> None:
    result = _assert_edge(tmp_path, "", "--http2", "HTTP/2", exit_code=7)
    assert (
        "  FAIL  edge headers over HTTP/2: GET https://nova.example.com/up failed" in result.stdout
    )
    assert result.stdout.endswith("rc=1\n")


def test_a_trailing_comment_on_a_brace_keeps_the_block_structure(tmp_path: Path) -> None:
    """A site closed by `} # end` is closed: a later site's header never becomes this one's."""
    path = tmp_path / "Caddyfile"
    path.write_text(
        f"{SITE} {{ # the router\n"
        '\theader X-Frame-Options "DENY"\n'
        "\trequest_body { # the outer fence\n\t\tmax_size 25MB\n\t} # request_body\n"
        '\theader Referrer-Policy "no-referrer"\n'
        f"}} # {SITE}\n\n"
        'john.example.com {\n\theader X-Frame-Options "SAMEORIGIN"\n}\n'
    )
    result = _read_site(path, SITE, tmp_path)
    assert result.returncode == 0, result.stdout
    assert result.stdout.splitlines() == ["X-Frame-Options\tDENY", "Referrer-Policy\tno-referrer"]


def test_a_curl_that_wrote_no_headers_fails_never_reads_an_earlier_answer(tmp_path: Path) -> None:
    """The HTTP/2 run reuses the dump path, so a silent curl must not inherit HTTP/1.1's."""
    caddyfile = _caddyfile(tmp_path, 'header X-Frame-Options "DENY"\n')
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "curl"
    fake.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake.chmod(0o755)
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "edge-headers.dump").write_text(GOOD_H2)
    result = _run(
        f"caddy_site_headers {caddyfile} {SITE}\n"
        f"assert_edge_headers https://{SITE}/up --http2 HTTP/2\necho rc=$rc\n",
        tmp_path,
        path=str(bindir),
    )
    assert "returned no headers to read" in result.stdout, result.stdout + result.stderr
    assert result.stdout.endswith("rc=1\n")
