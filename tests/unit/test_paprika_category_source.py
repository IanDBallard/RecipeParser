"""PaprikaCategorySource: a database that cannot be read fails the run; a file
that is simply not a Paprika database (no ZCATEGORY table) offers nothing."""
from __future__ import annotations

import sqlite3

import pytest

from recipeparser.exceptions import CategoryLoadError
from recipeparser.io.category_sources.paprika_db import PaprikaCategorySource


def _db(path, with_table: bool = True):
    con = sqlite3.connect(str(path))
    if with_table:
        con.execute("CREATE TABLE ZCATEGORY (Z_PK INTEGER, ZNAME TEXT, ZPARENTCATEGORY INTEGER)")
        con.executemany(
            "INSERT INTO ZCATEGORY VALUES (?, ?, ?)",
            [(1, "Cuisine", None), (2, "Italian", 1)],
        )
    else:
        con.execute("CREATE TABLE OTHER (x INTEGER)")
    con.commit()
    con.close()
    return path


def test_categories_load(tmp_path):
    src = PaprikaCategorySource(_db(tmp_path / "paprika.db"))
    assert src.load_axes() == {"Cuisine": ["Italian"]}


def test_no_zcategory_table_offers_nothing(tmp_path):
    src = PaprikaCategorySource(_db(tmp_path / "paprika.db", with_table=False))
    assert src.load_axes() == {}


def test_missing_file_offers_nothing(tmp_path):
    assert PaprikaCategorySource(tmp_path / "nope.db").load_axes() == {}


def test_a_file_that_is_not_a_database_raises(tmp_path):
    path = tmp_path / "paprika.db"
    path.write_bytes(b"this is not a sqlite database at all, just text " * 20)
    with pytest.raises(CategoryLoadError, match="paprika.db"):
        PaprikaCategorySource(path).load_axes()
