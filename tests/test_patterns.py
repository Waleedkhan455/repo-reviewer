import pytest

from src.repo import Snapshot, list_files
from src.tools.patterns import mask_secrets, scan_patterns


@pytest.mark.parametrize("line, rule", [
    ("result = eval(user_input)", "UNSAFE-EVAL"),
    ("exec(code)", "UNSAFE-EXEC"),
    ("subprocess.run(cmd, shell=True)", "UNSAFE-SHELL"),
    ("os.system('ls ' + d)", "UNSAFE-OSSYSTEM"),
    ("data = pickle.loads(blob)", "UNSAFE-PICKLE"),
    ("cfg = yaml.load(f)", "UNSAFE-YAML"),
    ("cur.execute(f\"SELECT * FROM t WHERE id={uid}\")", "SQL-CONCAT"),
    ("cur.execute(\"SELECT * FROM t WHERE id=%s\" % uid)", "SQL-CONCAT"),
    ("q = f\"DELETE FROM t WHERE id={uid}\"", "SQL-FSTRING"),
    ("KEY = 'AKIAABCDEFGHIJKLMNOP'", "SECRET-AWS"),
    ("db_password = 'hunter22'", "SECRET-ASSIGN"),
    ('jwt_secret: str = "insecure-development-secret-change-me"', "SECRET-ASSIGN"),
    ('api_key: str | None = "abcdef123456"', "SECRET-ASSIGN"),
])
def test_detects(make_repo, line, rule):
    root = make_repo({"a.py": line + "\n"})
    hits = scan_patterns(Snapshot(root, list_files(root)))
    assert rule in {h.rule for h in hits}


@pytest.mark.parametrize("line", [
    "model.eval()",
    "cur.execute('SELECT * FROM t WHERE id=?', (uid,))",
    "yaml.load(f, Loader=yaml.SafeLoader)",
    "password = os.environ['PASSWORD']",
    "admin_password: str | None = None",
    "def check_password(password: str) -> bool:",
    "subprocess.run(['ls', d])",
])
def test_no_false_positive(make_repo, line):
    root = make_repo({"a.py": line + "\n"})
    assert scan_patterns(Snapshot(root, list_files(root))) == []


def test_mask_secrets():
    masked = mask_secrets("key = 'AKIAABCDEFGHIJKLMNOP'; db_password = \"hunter22\"")
    assert "ABCDEFGHIJKLMNOP" not in masked
    assert "hunter22" not in masked
    assert "AKIA****" in masked
