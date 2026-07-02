"""Verify the Board outline row in the Overlays control.

Builds the real PdnViewer overlay-row widgets headless and checks:
  * the board-outline row has the "show everywhere" eye in the SAME
    column (layout index 1) as every other row,
  * it has NO "show on selected rails only" eye (index 0 is an inert
    spacer, not an EyeButton),
  * it has NO fill toggle (the trailing slot is a spacer),
  * a normal row (pads) still has both eyes + a fill toggle.

Run:  python _verify_board_outline_row.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main() -> int:
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])  # noqa: F841

    from fypa.altium_viewer import PdnViewer, EyeButton, FillToggleButton

    class _Stub:
        pass

    s = _Stub()
    PdnViewer._init_overlay_state(s)

    # Keep the row QWidgets alive for the whole check — dropping them
    # deletes their children out from under us.
    rows = {}

    def build(key, variant, label, splittable):
        w = PdnViewer._build_overlay_row_widget(
            s, key, variant, label, splittable=splittable)
        rows[key] = w
        lay = w.layout()
        return [lay.itemAt(i).widget() for i in range(lay.count())]

    ok = True

    # --- board outline row ---------------------------------------------
    bo = build("board_outline", "both", "Board outline", False)
    eye_idx1 = isinstance(bo[1], EyeButton)
    no_rails_eye = not isinstance(bo[0], EyeButton)
    no_fill = not any(isinstance(x, FillToggleButton) for x in bo)
    same_width = bo[0].width() == bo[1].width()
    print(f"board_outline row widgets: {[type(x).__name__ for x in bo]}")
    print(f"  show-everywhere eye at index 1 .......... "
          f"{'OK' if eye_idx1 else 'FAIL'}")
    print(f"  no rails-only eye (index 0 is spacer) ... "
          f"{'OK' if no_rails_eye else 'FAIL'}")
    print(f"  rails slot width matches eye width ...... "
          f"{'OK' if same_width else 'FAIL'} "
          f"({bo[0].width()} vs {bo[1].width()})")
    print(f"  no fill toggle .......................... "
          f"{'OK' if no_fill else 'FAIL'}")
    ok &= eye_idx1 and no_rails_eye and no_fill and same_width

    # --- a normal row (pads) for contrast ------------------------------
    pads = build("pads", "both", "Pads", True)
    pads_two_eyes = (isinstance(pads[0], EyeButton)
                     and isinstance(pads[1], EyeButton))
    pads_has_fill = any(isinstance(x, FillToggleButton) for x in pads)
    aligned = pads[0].width() == bo[0].width()
    print(f"pads row widgets: {[type(x).__name__ for x in pads]}")
    print(f"  two eyes ................................ "
          f"{'OK' if pads_two_eyes else 'FAIL'}")
    print(f"  has fill toggle ......................... "
          f"{'OK' if pads_has_fill else 'FAIL'}")
    print(f"  eye column aligns with board_outline .... "
          f"{'OK' if aligned else 'FAIL'}")
    ok &= pads_two_eyes and pads_has_fill and aligned

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
