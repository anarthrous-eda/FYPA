"""A rail solved under a different member's name must not show as pending.

Regression: Mangala's +12V group was solved and listed as HS_SNS, but the
pending check named the group "+12V" (the '+'-prefixed preference) and, not
finding that name among the solved rails, also listed "+12V (unsolved)".
"""
from types import SimpleNamespace

from fypa.viewer.editor.pending import _PendingRailsMixin


class _Host(_PendingRailsMixin):
    def __init__(self, groups, rail_names, rail_to_members, merged=None):
        self._groups = groups
        self._rail_names = rail_names
        self._rail_to_members = rail_to_members
        self._merged_children = merged or {}
        self._project = SimpleNamespace(editor_directives=[
            SimpleNamespace(role="SOURCE", p_net="+12V"),
            SimpleNamespace(role="SINK", p_net="HS_SNS"),
        ])

    def _connected_nets(self, net):
        for g in self._groups:
            if net in g:
                return set(g)
        return {net}


def test_solved_under_other_member_name_is_not_pending():
    host = _Host([{"+12V", "HS_SNS"}], ["HS_SNS"],
                 {"HS_SNS": ["HS_SNS", "+12V"]})
    assert host._editor_pending_rails() == {}


def test_solved_via_merged_section_is_not_pending():
    host = _Host([{"+12V", "HS_SNS"}], ["HS_SNS"], {"HS_SNS": ["HS_SNS"]},
                 merged={"HS_SNS": ["+12V"]})
    assert host._editor_pending_rails() == {}


def test_unsolved_group_still_pending():
    host = _Host([{"+12V", "HS_SNS"}], ["+3V3"], {"+3V3": ["+3V3"]})
    assert host._editor_pending_rails() == {"+12V": ["+12V", "HS_SNS"]}
