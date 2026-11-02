"""訳文の数字チェック (D-69): static/numcheck.js, run with Node when it is installed."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "newsreader" / "static" / "numcheck.js"
CASES = [
    ("At least 53 people were killed and 200 injured.", "少なくとも53人が死亡し、200人が負傷した。", []),
    ("At least 53 people were killed and 200 injured.", "少なくとも50人が死亡し、200人が負傷した。", ["53"]),
    ("The deal is worth $1.2 million.", "取引額は120万ドル。", []),
    ("최소 53명이 숨지고 1만2천명이 대피했다.", "少なくとも53人が死亡し、1万2000人が避難した。", []),
    ("至少53人死亡，逾200人受傷。", "At least 53 people died and more than 200 were injured.", []),
    ("In 2026, the G7 met 3 times.", "2026年、G7は3回会合した。", []),
    ("He was 1 of 2 survivors.", "彼は二人の生存者の一人だった。", []),
    ("Prices rose 15% in 2025.", "2025年に物価は上昇した。", ["15"]),
    # A comma after a figure is punctuation, not part of it.
    ("Pike, 50, who killed her in 1995, was 12,000 km away.", "パイクは1995年に彼女を殺害した。", ["50", "12,000"]),
]


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
class NumberCheckTests(unittest.TestCase):
    def test_cases(self):
        program = (
            f"const nc = require({json.dumps(str(SCRIPT))});"
            f"console.log(JSON.stringify({json.dumps([[a, b] for a, b, _ in CASES])}.map(([a, b]) => nc.missing(a, b))));"
        )
        out = json.loads(subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(out, [expected for _, _, expected in CASES])


if __name__ == "__main__":
    unittest.main()
