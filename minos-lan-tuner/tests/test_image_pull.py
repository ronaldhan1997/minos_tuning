"""Image download progress. No registry calls."""

from apps.worker.image_pull import apply_event, image_query, progress_line, read_events
from apps.worker.windows_client import progress_percent


def test_image_query_keeps_tag_and_digest() -> None:
    assert image_query("broadinstitute/gatk:4.5.0.0").endswith(
        "fromImage=broadinstitute%2Fgatk&tag=4.5.0.0"
    )
    digest = "genonet/hap-py@sha256:03acabe84bbfba35f5a7234129d524c563f5657e1f21150a2ea2797f8e6d05f2"
    query = image_query(digest)
    assert "fromImage=genonet%2Fhap-py" in query
    assert "tag=sha256%3A03acabe" in query
    quay = "quay.io/biocontainers/bcftools:1.20--h8b25389_0"
    assert "tag=1.20--h8b25389_0" in image_query(quay)


def test_progress_line_sums_layers() -> None:
    layers: dict[str, dict[str, int | str]] = {}
    image = "broadinstitute/gatk:4.5.0.0"
    first = apply_event(
        image,
        layers,
        {"status": "Downloading", "id": "aaa", "progressDetail": {"current": 500, "total": 1000}},
    )
    assert first is not None
    assert "50%" in first
    second = apply_event(
        image,
        layers,
        {"status": "Downloading", "id": "bbb", "progressDetail": {"current": 250, "total": 1000}},
    )
    assert second == "Downloading broadinstitute/gatk:4.5.0.0 — 37% (750 B / 2.0 KB)"
    apply_event(image, layers, {"status": "Download complete", "id": "aaa"})
    apply_event(image, layers, {"status": "Download complete", "id": "bbb"})
    apply_event(
        image,
        layers,
        {"status": "Extracting", "id": "aaa", "progressDetail": {"current": 1000, "total": 1000}},
    )
    line = progress_line(image, layers)
    assert line.startswith("Extracting ")
    assert progress_percent(line) == 100
    assert progress_percent("Connected. Leave this window open.") is None
    assert progress_percent("Downloading broadinstitute/gatk:4.5.0.0") is None


def test_read_events_decodes_a_chunked_pull_response() -> None:
    body = b'{"status":"Downloading","id":"abc","progressDetail":{"current":1,"total":2}}\n'
    raw = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
        + format(len(body), "x").encode()
        + b"\r\n"
        + body
        + b"\r\n0\r\n\r\n"
    )
    events = read_events(raw)
    assert events[0]["id"] == "abc"
    assert events[0]["progressDetail"]["total"] == 2
