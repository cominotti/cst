# SPDX-License-Identifier: MPL-2.0
"""Unit tests for tests/quint/itf.py, the reader of ITF traces the replay tests use."""

from __future__ import annotations

import os
import sys
import unittest

# Run directly (python3 -I -B tests/quint/tests/test_itf.py): the checkout root holds the tests package.
_CHECKOUT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _CHECKOUT not in sys.path:
    sys.path.insert(0, _CHECKOUT)

from tests.quint.itf import itf, items, number, record, text, truth


class ItfTest(unittest.TestCase):
    def test_values_as_python(self) -> None:
        got = itf(
            {
                "n": {"#bigint": "12"},
                "m": {"#map": [["vda", {"#bigint": "0"}]]},
                "s": {"#set": [{"#tup": ["vda", {"#bigint": "1"}]}, {"#tup": ["vdb", {"#bigint": "2"}]}]},
                "l": ["a", True],
                "#meta": {"index": 0},
            }
        )
        self.assertEqual(got, {"n": 12, "m": {"vda": 0}, "s": [["vda", 1], ["vdb", 2]], "l": ["a", True]})
        self.assertEqual(itf({"#set": []}), [])

    def test_a_value_of_another_shape_fails(self) -> None:
        for bad in ({"#set": "x"}, {"#tup": 1}, {"#map": [["k"]]}, 1.5):
            with self.subTest(bad=bad), self.assertRaises(AssertionError):
                itf(bad)
        for reader, value in ((record, 1), (text, 1), (number, True), (truth, 1), (items, "x")):
            with self.subTest(reader=reader.__name__), self.assertRaises(AssertionError):
                reader(value)


if __name__ == "__main__":
    unittest.main()
