"""The resolve log summarises skipped copper renames in one line.

Auto-minted ``$NET_xxx`` names on isolated no-net pads can't be applied by
the solver, and a board can have hundreds of them; one WARNING per name
buried the rest of the resolve log.
"""
from fypa.editor_directives import summarize_copper_name_warnings


def test_no_warnings_gives_no_summary():
    assert summarize_copper_name_warnings([]) is None


def test_summary_counts_by_reason_most_common_first():
    warnings = [
        "Copper rename '$NET_001': no unnamed copper on layer 8; skipped.",
        "Copper rename '$NET_002': no unnamed copper on layer 5; skipped.",
        "Copper rename '$NET_003': no unnamed copper on layer 32; skipped.",
        "Copper rename '$NET_004': anchor (44.0737, 18.58) is not on a "
        "NO_NET copper polygon on layer 9; skipped.",
        "Copper rename '$NET_005': layer 40 is not in the enabled copper "
        "stack; skipped.",
        "Copper rename '$NET_006': something new; skipped.",
    ]
    summary = summarize_copper_name_warnings(warnings)
    assert summary.startswith("6 copper name(s) not applied (")
    assert ("3 no unnamed copper on their layer, "
            "1 anchor not on unnamed copper") in summary
    assert "1 layer not in the copper stack" in summary
    assert "1 other" in summary
    assert "--debug" in summary
