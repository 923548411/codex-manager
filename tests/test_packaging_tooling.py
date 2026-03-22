from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_build_bat_is_ascii_only_for_cmd_compatibility():
    build_bat = REPO_ROOT / "build.bat"

    content = build_bat.read_bytes()

    try:
        content.decode("ascii")
    except UnicodeDecodeError as exc:  # pragma: no cover - exercised on failure
        raise AssertionError("build.bat 应保持 ASCII，避免 cmd 编码误解析") from exc


def test_build_bat_installs_project_requirements_before_pyinstaller():
    build_bat = (REPO_ROOT / "build.bat").read_text(encoding="ascii")

    assert "requirements.txt" in build_bat
    assert "PyInstaller codex_register.spec --clean --noconfirm" in build_bat or (
        "pyinstaller codex_register.spec --clean --noconfirm" in build_bat
    )
    assert "codex-register-windows-X64.exe" in build_bat


def test_dockerignore_excludes_large_local_build_contexts():
    dockerignore = REPO_ROOT / ".dockerignore"

    assert dockerignore.exists(), "需要 .dockerignore 来缩小 Docker 构建上下文"
    content = dockerignore.read_text(encoding="utf-8")

    assert ".git" in content
    assert "build/" in content
    assert "dist/" in content


def test_dockerfile_build_avoids_copying_entire_repository_context():
    dockerfile = (REPO_ROOT / "Dockerfile.build").read_text(encoding="utf-8")

    assert "COPY . ." not in dockerfile
    assert "COPY requirements.txt" in dockerfile
