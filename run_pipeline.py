"""투자설명서 PDF의 추출·서버 OCR·문서 구조·검토 HTML을 한 번에 만든다.

사용법: python run_pipeline.py [파일/폴더 ...] [--force] [--skip-ocr]
기본 입력은 스크립트 옆 inbox/, 결과는 output/<파일명>/에 저장한다.
폴더는 하위 폴더까지 검색하며 .pdf와 .pdf.raw만 처리한다.
--skip-ocr는 기존 <파일명>.server_ocr.json을 그대로 재사용한다.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INBOX = ROOT / "inbox"
OUTPUT = ROOT / "output"


def pdf_stem(path):
    return re.sub(r"\.pdf(?:\.raw)?$", "", path.name, flags=re.I)


def is_pdf_name(path):
    return path.name.lower().endswith((".pdf", ".pdf.raw"))


def discover(paths):
    """중복 입력과 생성된 output 폴더를 제외하고 입력 파일을 모은다."""
    found = set()
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(f"입력 경로가 없습니다: {path}")
        candidates = path.rglob("*") if path.is_dir() else [path]
        for candidate in candidates:
            if candidate.is_file() and is_pdf_name(candidate):
                resolved = candidate.resolve()
                if not resolved.is_relative_to(OUTPUT):
                    found.add(resolved)
    return sorted(found, key=lambda p: str(p).casefold())


def fresh(outputs, inputs):
    return bool(outputs) and all(p.is_file() for p in outputs) and (
        min(p.stat().st_mtime_ns for p in outputs)
        >= max(p.stat().st_mtime_ns for p in inputs)
    )


def image_files(layer1, embedded=False):
    data = json.loads(layer1.read_text(encoding="utf-8"))
    files = [p["file"] for p in data["pages"]]
    if embedded:
        files.extend(b["file"] for p in data["pages"] for b in p["blocks"]
                     if b.get("type") == "image" and b.get("file"))
    return [layer1.parent / name for name in files]


def ocr_environment():
    """셸 환경값 > 저장소 루트 .env > Windows 사용자 환경값 순으로 읽는다. 값은 출력하지 않는다."""
    env = os.environ.copy()
    names = ("OCR_API_KEY", "OCR_BASIC_AUTH", "OCR_SERVER")
    dotenv = ROOT / ".env"
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8-sig").splitlines():
            name, sep, value = line.partition("=")
            name, value = name.strip(), value.strip().strip('"').strip("'")
            if sep and name in names and value and not env.get(name):
                env[name] = value
    if os.name == "nt" and any(not env.get(name) for name in names):
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                for name in names:
                    if not env.get(name):
                        try:
                            value, _ = winreg.QueryValueEx(key, name)
                            if isinstance(value, str) and value:
                                env[name] = value
                        except OSError:
                            pass
        except OSError:
            pass
    missing = [name for name in names if not env.get(name)]
    if missing:
        raise ValueError("OCR 환경변수가 없습니다: " + ", ".join(missing))
    return env


def step(label, script, arguments, outputs, inputs, force, env=None):
    started = time.perf_counter()
    if not force and fresh(outputs, inputs):
        print(f"  {label}: 최신 결과 사용 ({time.perf_counter() - started:.1f}s)", flush=True)
        return
    # 인증은 실제 서버 요청이 필요한 경우에만 확인한다.
    if Path(script).name == "ocr_pages_server.py":
        env = ocr_environment()
    child_env = dict(os.environ if env is None else env, PYTHONIOENCODING="utf-8")
    print(f"  {label}: 실행", flush=True)
    result = subprocess.run(
        [sys.executable, "-I", str(ROOT / script), *map(str, arguments)],
        cwd=ROOT, env=child_env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode:
        detail = (result.stderr or result.stdout)[-2000:].strip()
        for name in ("OCR_API_KEY", "OCR_BASIC_AUTH", "OCR_SERVER"):
            if child_env.get(name):
                detail = detail.replace(child_env[name], "[비공개]")
        raise RuntimeError(f"{label} 실패 (종료 코드 {result.returncode}): {detail}")
    if not all(p.is_file() for p in outputs):
        raise RuntimeError(f"{label}: 출력 파일이 생성되지 않았습니다")
    print(f"  {label}: 완료 ({time.perf_counter() - started:.1f}s)", flush=True)


def process(pdf, force=False, skip_ocr=False):
    with pdf.open("rb") as source:
        if source.read(4) != b"%PDF":
            print("  건너뜀: PDF 헤더(%PDF)가 없습니다", flush=True)
            return "skipped"
    stem = pdf_stem(pdf)
    folder = OUTPUT / stem
    folder.mkdir(parents=True, exist_ok=True)
    layer1 = folder / f"{stem}.json"
    images_dir = folder / f"{stem}_images"
    ocr = folder / f"{stem}.server_ocr.json"
    structure = folder / f"{stem}.structure.json"
    ocr_structure = folder / f"{stem}.ocr_structure.json"
    review = folder / f"{stem}.ocr_review.html"
    if skip_ocr and not ocr.is_file():
        raise FileNotFoundError(f"--skip-ocr에 필요한 기존 결과가 없습니다: {ocr}")

    # JSON뿐 아니라 렌더링된 페이지 및 추출 이미지 누락도 확인한다.
    extracted = [layer1]
    if layer1.is_file():
        try:
            extracted.extend(image_files(layer1, embedded=True))
        except (ValueError, KeyError, TypeError):
            force_extract = True
        else:
            force_extract = False
    else:
        force_extract = False
    step("추출", "ETF_FUND_ocr/extract_pdf_json.py", [pdf, "-o", layer1, "--images-dir", images_dir],
         extracted, [pdf, ROOT / "ETF_FUND_ocr" / "extract_pdf_json.py"], force or force_extract)
    page_images = image_files(layer1)
    if not page_images or not all(p.is_file() for p in page_images):
        raise ValueError("추출 결과의 페이지 이미지가 없거나 누락되었습니다")
    if skip_ocr:
        print("  서버 OCR: 기존 결과 재사용 (--skip-ocr)", flush=True)
    else:
        step("서버 OCR", "ETF_FUND_ocr/ocr_pages_server.py", [pdf, layer1, "-o", ocr], [ocr],
             [pdf, layer1, ROOT / "ETF_FUND_ocr" / "ocr_pages_server.py"], force)
    structure_scripts = [ROOT / "ETF_FUND_postprocess" / "build_doc_structure.py", ROOT / "ETF_FUND_ocr" / "extract_pdf_json.py"]
    step("PDF 구조", "ETF_FUND_postprocess/build_doc_structure.py", [pdf, layer1, "--ocr", ocr, "-o", structure],
         [structure], [pdf, layer1, ocr, *structure_scripts], force)
    step("PDF 구조 HTML", "ETF_FUND_postprocess/render_structure.py", [structure, "-o", structure.with_suffix(".html")],
         [structure.with_suffix(".html")], [structure, ROOT / "ETF_FUND_postprocess" / "render_structure.py"], force)
    step("OCR 구조", "ETF_FUND_postprocess/build_doc_structure_ocr.py", [layer1, ocr, "-o", ocr_structure],
         [ocr_structure], [layer1, ocr, *page_images, *structure_scripts,
                           ROOT / "ETF_FUND_postprocess" / "build_doc_structure_ocr.py"], force)
    fields = folder / f"{stem}.fields.json"
    step("ETF 필드", "ETF_FUND_extract/extract_fields.py", [ocr_structure], [fields],
         [ocr_structure, ROOT / "ETF_FUND_extract" / "extract_fields.py"], force)
    step("OCR 구조 HTML", "ETF_FUND_postprocess/render_structure.py", [ocr_structure, "-o", ocr_structure.with_suffix(".html")],
         [ocr_structure.with_suffix(".html")], [ocr_structure, ROOT / "ETF_FUND_postprocess" / "render_structure.py"], force)
    step("OCR 검토", "ETF_FUND_postprocess/review_ocr_diff.py", [pdf, layer1, structure, ocr_structure, "-o", review],
         [review, folder / f"{stem}.review.pdf.raw"],
         [pdf, layer1, structure, ocr_structure, *page_images, ROOT / "ETF_FUND_postprocess" / "review_ocr_diff.py"], force)
    return "completed"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, help="입력 PDF 또는 폴더 (기본: inbox/)")
    parser.add_argument("--force", action="store_true", help="모든 단계를 다시 실행")
    parser.add_argument("--skip-ocr", action="store_true", help="기존 서버 OCR JSON 재사용")
    args = parser.parse_args()
    INBOX.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    counts = Counter()
    files = set()
    for path in args.paths or [INBOX]:
        try:
            files.update(discover([path]))
        except OSError as error:
            print(f"입력 오류: {error}", flush=True)
            counts["failed"] += 1
    owners = {}
    for pdf in sorted(files, key=lambda p: str(p).casefold()):
        print(f"[{pdf.name}]", flush=True)
        mark = time.perf_counter()
        try:
            key = pdf_stem(pdf).casefold()
            if key in owners:
                raise ValueError(f"출력 폴더 이름 중복: {owners[key]} / {pdf}")
            status = process(pdf, args.force, args.skip_ocr)
            if status == "completed":
                owners[key] = pdf
            counts[status] += 1
        except Exception as error:
            counts["failed"] += 1
            print(f"  실패: {error}", flush=True)
        print(f"  파일 처리: {time.perf_counter() - mark:.1f}s", flush=True)
    sys.path.insert(0, str(ROOT / "ETF_FUND_extract"))
    from extract_fields import rebuild_datalake
    try:
        print(f"데이터레이크: {rebuild_datalake(OUTPUT)}개 문서", flush=True)
    except Exception as error:
        counts["failed"] += 1
        print(f"데이터레이크 실패: {error}", flush=True)
    print(f"요약: 완료 {counts['completed']}, 건너뜀 {counts['skipped']}, "
          f"실패 {counts['failed']} ({time.perf_counter() - started:.1f}s)", flush=True)
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
