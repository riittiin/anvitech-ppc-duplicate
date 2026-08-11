from engine import book_store


def test_absence_crud_round_trip():
    a = book_store.save_absence({"operator": "Mahesh",
                                 "from_date": "2026-07-16", "to_date": "2026-07-18"})
    assert a["id"] and book_store.load_absences() == [a]
    assert book_store.delete_absence(a["id"]) is True
    assert book_store.load_absences() == []
    assert book_store.delete_absence("nope") is False
