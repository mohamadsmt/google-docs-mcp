"""Images preserve named-range identity while Google shifts body coordinates."""
from copy import deepcopy

import pytest

from google_docs_mcp.client import DocsMCPError
from test_images import Client, document, image_after, insert, payload


def named_ranges(doc):
    return payload(doc)["namedRanges"]["tracked"]["namedRanges"][0]["ranges"]


def source():
    before = document("abc target\n")
    payload(before)["namedRanges"] = {"tracked": {"namedRanges": [
        {"namedRangeId": "n.1", "name": "tracked", "ranges": [
            {"startIndex": 5, "endIndex": 11, "tabId": "tab-1"},
            {"startIndex": 5, "endIndex": 11, "segmentId": "header-1", "tabId": "tab-1"},
        ]}]}}
    return before


@pytest.mark.parametrize("profile", ["plain", "persian"])
@pytest.mark.parametrize("position,anchor,index,start,end", [
    ("start", None, 1, 6, 12),
    ("before", "target", 5, 6, 12),
    ("before", "get", 8, 5, 12),
    ("after", "target", 11, 5, 11),
    ("after", "target", 11, 5, 12),
])
def test_body_named_range_mechanical_shift_preserves_coverage(profile, position, anchor, index, start, end):
    before = source()
    after = image_after(before, index, profile=profile)
    named_ranges(after)[0].update(startIndex=start, endIndex=end)
    client = Client(before, after)
    args = {"position": position, "anchor_text": anchor, "format_profile": profile}
    assert insert(client, **args)["valid"] is True
    assert client.writes == []
    assert insert(client, **args, apply=True)["verified"] is True
    assert len(client.writes) == 1
    assert client.before == before


@pytest.mark.parametrize("change", ["coverage", "id", "header"])
def test_unrelated_named_range_changes_are_not_hidden(change):
    before = source()
    after = image_after(before, 1, profile="plain")
    named_ranges(after)[0].update(startIndex=6, endIndex=12)
    if change == "coverage":
        named_ranges(after)[0]["endIndex"] = 13
    elif change == "id":
        payload(after)["namedRanges"]["tracked"]["namedRanges"][0]["namedRangeId"] = "other"
    else:
        named_ranges(after)[1]["endIndex"] = 12
    client = Client(before, after)
    with pytest.raises(DocsMCPError) as caught:
        insert(client, position="start", format_profile="plain", apply=True)
    assert caught.value.code == "verification_failed" and len(client.writes) == 1


@pytest.mark.parametrize("bad", [[], {"tracked": []}, {"tracked": {"namedRanges": [None]}}])
def test_malformed_named_ranges_rejected_before_preview_or_write(bad):
    before = source()
    payload(before)["namedRanges"] = deepcopy(bad)
    client = Client(before)
    with pytest.raises(DocsMCPError) as caught:
        insert(client, position="start", apply=True)
    assert caught.value.code == "invalid_input" and client.writes == []
