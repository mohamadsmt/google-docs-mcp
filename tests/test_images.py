"""Synthetic, no-network contract for revision-safe inline image insertion."""
from copy import deepcopy
import importlib
import inspect
import json

import pytest

from google_docs_mcp.client import DocsMCPError
from test_insertion import Client as TextClient, DOC, STYLE, body, document, width

URI = "https://images.example.com/chart.png?size=large%2Fwide"
IMAGE = "new-image"


def insert(client, **kwargs):
    # Import inside the call so absence is a feature failure, not collection failure.
    try:
        module = importlib.import_module("google_docs_mcp.images")
    except ModuleNotFoundError:
        pytest.fail("image insertion module is not implemented")
    return module.insert_image(client, document=DOC, image_uri=kwargs.pop("image_uri", URI),
                               expected_revision_id=kwargs.pop("expected_revision_id", "rev-1"), **kwargs)


def payload(doc):
    return doc["tabs"][0]["documentTab"]


def embedded(w=120, h=60):
    return {"inlineObjectProperties": {"embeddedObject": {
        "size": {"width": {"magnitude": w, "unit": "PT"},
                 "height": {"magnitude": h, "unit": "PT"}},
        "imageProperties": {"sourceUri": "", "contentUri": "SECRET_TEMP_BEARER"},
    }}}


def image_after(before, index, *, w=120, h=60, profile="plain"):
    """Independent fixture transform; splitting text retains its entire style."""
    after = deepcopy(before)
    after["revisionId"] = "rev-2"
    selected = payload(after)
    def walk(value):
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for key in ("startIndex", "endIndex"):
                if key in value and (value[key] > index or (key == "startIndex" and value[key] == index)):
                    value[key] += 1
            for item in value.values():
                if isinstance(item, (dict, list)):
                    walk(item)
    # Keep paragraph/ancestor start boundaries fixed at the insertion point.
    walk(selected["body"])
    def locate(content):
        for node in content:
            if "table" in node:
                for row in node["table"]["tableRows"]:
                    for cell in row["tableCells"]:
                        found = locate(cell["content"])
                        if found:
                            return found
            if "paragraph" in node and node["startIndex"] <= index + 1 < node["endIndex"]:
                return node
        return None
    node = locate(selected["body"]["content"])
    if node["startIndex"] == index + 1:
        node["startIndex"] = index
    elements = node["paragraph"]["elements"]
    fresh = {"startIndex": index, "endIndex": index + 1,
             "inlineObjectElement": {"inlineObjectId": IMAGE}}
    for offset, element in enumerate(elements):
        original_start = element["startIndex"] - (element["startIndex"] > index)
        if "textRun" in element and original_start <= index < element["endIndex"]:
            raw = element["textRun"]["content"].encode("utf-16-le")
            split = (index - original_start) * 2
            left, right = deepcopy(element), deepcopy(element)
            left.update(startIndex=original_start, endIndex=index)
            left["textRun"]["content"] = raw[:split].decode("utf-16-le")
            right.update(startIndex=index + 1)
            right["textRun"]["content"] = raw[split:].decode("utf-16-le")
            elements[offset:offset + 1] = ([left] if split else []) + [fresh, right]
            break
        if original_start == index:
            elements.insert(offset, fresh)
            break
    else:
        raise AssertionError("fixture insertion missing")
    if profile == "persian":
        node["paragraph"].setdefault("paragraphStyle", {}).update(deepcopy(STYLE))
    selected.setdefault("inlineObjects", {})[IMAGE] = embedded(w, h)
    return after


class Client(TextClient):
    def batch_update(self, *args, **kwargs):
        result = super().batch_update(*args, **kwargs)
        result["replies"][0] = {"insertInlineImage": {"objectId": IMAGE}}
        return result


@pytest.mark.parametrize("position,anchor,index", [
    ("start", None, 1), ("end", None, 16),
    ("before", "هدف", 9), ("after", "هدف", 12),
])
def test_exact_utf16_positions_preview_and_one_atomic_apply(position, anchor, index):
    before = document("سلام 🧪 هدف\nبعد\n")
    client = Client(before, image_after(before, index))
    preview = insert(client, position=position, anchor_text=anchor, format_profile="plain")
    assert preview["valid"] and preview["applied"] is False and preview["index"] == index
    assert client.writes == []
    result = insert(client, position=position, anchor_text=anchor, format_profile="plain", apply=True)
    assert result["verified"] and result["image_id"] == IMAGE
    assert result["range"] == {"startIndex": index, "endIndex": index + 1, "tabId": "tab-1"}
    assert result["before_revision_id"] == "rev-1" and result["after_revision_id"] == "rev-2"
    assert "SECRET" not in json.dumps(result) and "contentUri" not in json.dumps(result)
    assert client.writes == [(DOC, [{"insertInlineImage": {
        "uri": URI, "location": {"index": index, "tabId": "tab-1"}}}], "rev-1")]
    assert client.events == ["metadata", "read", "metadata", "read", "write", "read"]


@pytest.mark.parametrize("uri", [None, 1, "", "http://images.example.com/x", "file:///x", "data:image/png,x",
    "https://u:p@images.example.com/x", "https://images.example.com:8443/x", "https://images.example.com:/x",
    "https://localhost/x", "https://foo.localhost/x", "https://printer.local/x", "https://intranet/x",
    "https://127.0.0.1/x", "https://10.1.2.3/x", "https://169.254.169.254/x", "https://100.64.0.1/x",
    "https://[::1]/x", "https://[fc00::1]/x", "https://[::ffff:127.0.0.1]/x", "https://127.1/x",
    "https://2130706433/x", "https://0x7f000001/x", "https://0177.0.0.1/x", "https://***@127.0.0.1/x",
    " https://images.example.com/x", "https://images.example.com/\n", "https://images.example.com/a b",
    "https://images.example.com/\x7f", "https://images.example.com/\u200b", "https://images.example.com/\ud800",
    "https://images.example.com/" + "a" * 2048, "https://images.example.com/" + "ژ" * 1024,
    "https://%31%32%37.0.0.1/x", "https://images.example.com/%0a", "https://images.example.com/%zz",
])
def test_invalid_urls_rejected_before_transport(uri):
    client = Client(document("target\n"))
    with pytest.raises(DocsMCPError) as error:
        insert(client, image_uri=uri, apply=True)
    assert error.value.code == "invalid_input" and client.events == []


@pytest.mark.parametrize("uri", [URI, "https://images.example.com:443/x", "https://8.8.8.8/x",
    "https://[2606:4700:4700::1111]/x", "https://images.example.com/نمودار.png",
    "https://images.example.com/" + "a" * (2048 - len("https://images.example.com/"))])
def test_public_uri_lexical_controls_no_dns_or_image_fetch(monkeypatch, uri):
    import socket
    import requests
    def forbidden(*args, **kwargs):
        pytest.fail("image URL must not be fetched or resolved locally")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(requests, "get", forbidden)
    assert insert(Client(document("target\n")), image_uri=uri)["valid"]


@pytest.mark.parametrize("key,value", [(key, value) for key in ("width_pt", "height_pt")
    for value in (True, "120", 0, -1, float("nan"), float("inf"), 1441, [], 10**500)])
def test_invalid_dimensions_pretransport(key, value):
    client = Client(document("x\n"))
    with pytest.raises(DocsMCPError) as error:
        insert(client, **{key: value}, apply=True)
    assert error.value.code == "invalid_input" and client.events == []


@pytest.mark.parametrize("kwargs,w,h", [({"width_pt": 120}, 120, 60), ({"height_pt": 60}, 120, 60),
    ({"width_pt": 120, "height_pt": 120}, 120, 60), ({"width_pt": 60, "height_pt": 20}, 40, 20)])
def test_aspect_ratio_bounding_box_not_naive_exact_dimensions(kwargs, w, h):
    before = document("x\n")
    client = Client(before, image_after(before, 2, w=w, h=h))
    assert insert(client, format_profile="plain", apply=True, **kwargs)["dimensions_verified"]
    size = client.writes[0][1][0]["insertInlineImage"]["objectSize"]
    assert size == {k.removesuffix("_pt"): {"magnitude": v, "unit": "PT"} for k, v in kwargs.items()}


@pytest.mark.parametrize("w,h", [(121, 60), (60, 60), (120, 121), (0, 60), (True, 60)])
def test_dimension_mismatch_after_write_requires_fresh_read(w, h):
    before = document("x\n")
    client = Client(before, image_after(before, 2, w=w, h=h))
    with pytest.raises(DocsMCPError) as error:
        insert(client, width_pt=120, height_pt=120, format_profile="plain", apply=True)
    assert error.value.code == "verification_failed" and "read" in str(error.value).lower()
    assert len(client.writes) == 1


def test_persian_layout_only_never_restyles_neighboring_text_runs():
    before = document("abc\nother\n")
    payload(before)["body"]["content"][0]["paragraph"]["elements"][0]["textRun"]["textStyle"] = {"bold": True}
    client = Client(before, image_after(before, 2, profile="persian"))
    assert insert(client, position="after", anchor_text="a", apply=True)["formatting_verified"]
    assert client.writes[0][1][1:] == [{"updateParagraphStyle": {
        "range": {"startIndex": 2, "endIndex": 3, "tabId": "tab-1"},
        "paragraphStyle": STYLE, "fields": "direction,alignment,indentStart,indentEnd"}}]


@pytest.mark.parametrize("key", ["direction", "alignment", "indentStart", "indentEnd"])
def test_each_layout_field_is_verified(key):
    before = document("x\n")
    after = image_after(before, 2, profile="persian")
    payload(after)["body"]["content"][0]["paragraph"]["paragraphStyle"].pop(key)
    client = Client(before, after)
    with pytest.raises(DocsMCPError) as error:
        insert(client, apply=True)
    assert error.value.code == "verification_failed"


@pytest.mark.parametrize("kwargs,code", [({"expected_revision_id": "stale"}, "stale_revision"),
    ({"position": "after", "anchor_text": "missing"}, "anchor_match_mismatch"),
    ({"position": "before", "anchor_text": "aa"}, "anchor_match_mismatch"),
    ({"position": "after", "anchor_text": "aa\nx"}, "invalid_input"),
    ({"position": []}, "invalid_input"), ({"format_profile": []}, "invalid_input"),
    ({"apply": "true"}, "invalid_input"), ({"expected_revision_id": ""}, "invalid_input")])
def test_revision_anchor_and_strict_shared_validation(kwargs, code):
    client = Client(document("aaa\nx\n"))
    with pytest.raises(DocsMCPError) as error:
        insert(client, **kwargs)
    assert error.value.code == code and client.writes == []


def multi_before():
    before = document("target\n")
    payload(before)["inlineObjects"] = {"old": embedded(20, 10)}
    payload(before)["positionedObjects"] = {"positioned": {"sentinel": "unchanged"}}
    payload(before)["headers"] = {"header": body("target\n", 0)}
    before["tabs"].append({"tabProperties": {"tabId": "other", "title": "Other"},
                           "documentTab": {"body": body("untouched\n")}})
    return before


def test_multitab_requires_selection_and_preserves_auxiliary_objects():
    before = multi_before()
    client = Client(before, image_after(before, 7))
    with pytest.raises(DocsMCPError) as error:
        insert(client)
    assert error.value.code == "multiple_tabs_require_tab_id"
    assert insert(client, tab_id="tab-1", format_profile="plain", apply=True)["verified"]


@pytest.mark.parametrize("fault", ["text", "style", "other_tab", "old_object", "positioned", "header",
    "extra_object", "missing_image", "not_image", "wrong_revision", "wrong_id", "wrong_index", "wrong_end", "tab_properties"])
def test_readback_rejects_each_unrelated_or_missing_change(fault):
    before = multi_before()
    after = image_after(before, 7)
    target = payload(after)
    node = target["body"]["content"][0]
    if fault == "text":
        node["paragraph"]["elements"][0]["textRun"]["content"] = "tamper"
    elif fault == "style":
        node["paragraph"]["elements"][0]["textRun"]["textStyle"]["bold"] = True
    elif fault == "other_tab":
        after["tabs"][1]["documentTab"]["body"] = body("changed\n")
    elif fault in ("old_object", "extra_object"):
        target["inlineObjects"]["old" if fault == "old_object" else "extra"] = embedded(55, 55)
    elif fault == "positioned":
        target["positionedObjects"] = {}
    elif fault == "header":
        target["headers"] = {}
    elif fault == "missing_image":
        del target["inlineObjects"][IMAGE]
    elif fault == "not_image":
        del target["inlineObjects"][IMAGE]["inlineObjectProperties"]["embeddedObject"]["imageProperties"]
    elif fault == "wrong_revision":
        after["revisionId"] = "other-revision"
    elif fault == "wrong_id":
        node["paragraph"]["elements"][1]["inlineObjectElement"]["inlineObjectId"] = "wrong"
    elif fault == "wrong_index":
        node["paragraph"]["elements"][1]["startIndex"] += 1
    elif fault == "wrong_end":
        node["endIndex"] += 1
    else:
        after["tabs"][0]["tabProperties"]["title"] = "changed"
    client = Client(before, after)
    with pytest.raises(DocsMCPError) as error:
        insert(client, tab_id="tab-1", format_profile="plain", apply=True)
    assert error.value.code == "verification_failed" and len(client.writes) == 1
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize("fault", ["timeout", "provider_error", "missing_id", "nonadvancing", "missing_revision", "bad_reply"])
def test_uncertain_write_is_never_replayed_and_errors_are_sanitized(fault):
    class Broken(Client):
        def batch_update(self, *args, **kwargs):
            result = super().batch_update(*args, **kwargs)
            if fault == "timeout":
                raise TimeoutError("SECRET_TEMP_BEARER")
            if fault == "provider_error":
                raise DocsMCPError("google_unavailable", "SECRET_TEMP_BEARER")
            if fault == "missing_id":
                result["replies"][0] = {}
            elif fault == "nonadvancing":
                result["writeControl"]["requiredRevisionId"] = "rev-1"
            elif fault == "missing_revision":
                result.pop("writeControl")
            elif fault == "bad_reply":
                result["replies"] = None
            return result
    before = document("x\n")
    client = Broken(before, image_after(before, 2))
    with pytest.raises(DocsMCPError) as error:
        insert(client, apply=True)
    assert "SECRET" not in str(error.value) and "read" in str(error.value).lower()
    assert len(client.writes) == 1


@pytest.mark.parametrize("profile", ["plain", "persian"])
def test_image_first_paragraph_preserves_old_inline_object(profile):
    before = document("\n")
    node = payload(before)["body"]["content"][0]
    node["endIndex"] = 3
    node["paragraph"]["elements"] = [
        {"startIndex": 1, "endIndex": 2, "inlineObjectElement": {"inlineObjectId": "old"}},
        {"startIndex": 2, "endIndex": 3, "textRun": {"content": "\n"}},
    ]
    payload(before)["inlineObjects"] = {"old": embedded(20, 10)}
    after = image_after(before, 1, profile=profile)
    # Google's short-lived download URI can rotate without object mutation.
    payload(after)["inlineObjects"]["old"]["inlineObjectProperties"]["embeddedObject"]["imageProperties"]["contentUri"] = "ROTATED_SECRET"
    assert insert(Client(before, after), position="start", format_profile=profile, apply=True)["verified"]


def test_table_cell_anchor_and_following_structure_are_preserved():
    before = document("\n")
    content = payload(before)["body"]["content"]
    content.append({"startIndex": 2, "endIndex": 15, "table": {"tableRows": [
        {"tableCells": [body("🧪target\n", start=5)]}
    ]}})
    content.extend(body("tail\n", start=15)["content"])
    after = image_after(before, 13, profile="persian")
    assert insert(Client(before, after), position="after", anchor_text="target", apply=True)["verified"]


def test_anchor_across_differently_styled_contiguous_runs():
    before = document("target\n")
    payload(before)["body"]["content"][0]["paragraph"]["elements"] = [
        {"startIndex": 1, "endIndex": 4, "textRun": {"content": "tar", "textStyle": {"bold": True}}},
        {"startIndex": 4, "endIndex": 8, "textRun": {"content": "get\n", "textStyle": {"italic": True}}},
    ]
    after = image_after(before, 7)
    assert insert(Client(before, after), position="after", anchor_text="target", format_profile="plain", apply=True)["verified"]


def test_headers_never_supply_the_anchor():
    before = document("body\n")
    payload(before)["headers"] = {"header": body("target\n", 0)}
    client = Client(before)
    with pytest.raises(DocsMCPError) as error:
        insert(client, position="after", anchor_text="target", apply=True)
    assert error.value.code == "anchor_match_mismatch" and client.writes == []


def test_persian_layout_accepts_google_omitted_zero_magnitudes():
    before = document("x\n")
    after = image_after(before, 2, profile="persian")
    style = payload(after)["body"]["content"][0]["paragraph"]["paragraphStyle"]
    for key in ("indentStart", "indentEnd"):
        style[key].pop("magnitude")
    assert insert(Client(before, after), apply=True)["formatting_verified"]


def test_returned_identity_must_be_new_across_document_tabs():
    before = multi_before()
    before["tabs"][1]["documentTab"]["inlineObjects"] = {IMAGE: embedded()}
    after = image_after(before, 7)
    client = Client(before, after)
    with pytest.raises(DocsMCPError) as error:
        insert(client, tab_id="tab-1", format_profile="plain", apply=True)
    assert error.value.code == "verification_failed" and len(client.writes) == 1


@pytest.mark.parametrize("failure", ["timeout", 503, 429])
def test_actual_google_client_transport_never_replays_image_posts(monkeypatch, failure):
    import requests
    from types import SimpleNamespace
    from google_docs_mcp.client import GoogleDocsClient
    fake = Client(document("x\n"))
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, url))
        assert "images.example.com" not in url
        if method == "GET":
            value = fake.before if "/documents/" in url else fake.drive_metadata(DOC)
            return SimpleNamespace(status_code=200, json=lambda: value)
        if failure == "timeout":
            raise requests.Timeout("SECRET_TEMP_BEARER")
        return SimpleNamespace(status_code=failure, headers={})
    monkeypatch.setattr("google_docs_mcp.client.time.sleep", lambda *_: None)
    with pytest.raises(DocsMCPError) as error:
        insert(GoogleDocsClient(SimpleNamespace(request=request)), apply=True)
    assert "SECRET" not in str(error.value)
    assert [method for method, _ in calls] == ["GET", "GET", "POST"]


@pytest.mark.parametrize("phase", ["write", "readback"])
@pytest.mark.parametrize("code,retryable", [("google_needs_reauth", False), ("permission_denied", False),
    ("rate_limited", True), ("document_not_found", False), ("google_unavailable", True)])
def test_known_upstream_error_codes_retryability_and_reauth_notice_survive_sanitization(phase, code, retryable):
    class Broken(Client):
        def batch_update(self, *args, **kwargs):
            result = super().batch_update(*args, **kwargs)
            if phase == "write":
                raise DocsMCPError(code, "SECRET_TEMP_BEARER", retryable=retryable)
            return result
        def get_document(self, *args, **kwargs):
            if phase == "readback" and self.writes:
                raise DocsMCPError(code, "SECRET_TEMP_BEARER", retryable=retryable)
            return super().get_document(*args, **kwargs)
    before = document("x\n")
    client = Broken(before, image_after(before, 2))
    with pytest.raises(DocsMCPError) as error:
        insert(client, format_profile="plain", apply=True)
    assert error.value.code == code and error.value.retryable is retryable
    assert "SECRET" not in str(error.value) and "read" in str(error.value).lower()
    assert error.value.__context__ is None and error.value.__cause__ is None
    if code == "google_needs_reauth":
        assert "Reconnect Google Docs" in str(error.value)
    assert len(client.writes) == 1


def test_exact_public_keyword_only_signature():
    insert(Client(document("x\n")))
    from google_docs_mcp.images import insert_image
    parameters = list(inspect.signature(insert_image).parameters.values())
    assert [p.name for p in parameters] == ["client", "document", "image_uri", "expected_revision_id", "position",
        "anchor_text", "tab_id", "width_pt", "height_pt", "format_profile", "apply"]
    assert all(p.kind == inspect.Parameter.KEYWORD_ONLY for p in parameters[1:])
