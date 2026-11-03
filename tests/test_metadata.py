import json

import pytest

from dbgen import metadata
from dbgen.errors import UsageError


def test_a_later_file_replaces_an_object_whole(tmp_path):
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    first.write_text(json.dumps({"userGroups": [{"id": "Group000001", "name": "A", "code": "A"}]}))
    second.write_text(json.dumps({"userGroups": [{"id": "Group000001", "name": "B"}]}))
    md = metadata.read([first, second])
    assert md.user_group("Group000001") == {"id": "Group000001", "name": "B"}


@pytest.mark.parametrize(
    ("element", "error"),
    [
        ({"id": "DataElem001", "valueType": "TEXT"}, "data element DataElem001 .* has no name"),
        ({"id": "DataElem001", "name": "Weight"}, "data element DataElem001 .* has no valueType"),
    ],
)
def test_an_object_missing_a_required_key_fails_naming_it(tmp_path, element, error):
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps({"dataElements": [element]}))
    with pytest.raises(UsageError, match=error):
        metadata.read([path]).data_element("DataElem001")


def test_a_program_without_a_tracked_entity_type_fails_naming_it(tmp_path):
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps({"programs": [{"id": "Program0001", "name": "Pregnancy"}]}))
    with pytest.raises(UsageError, match="program Program0001 .* has no trackedEntityType.id"):
        metadata.read([path]).programs  # noqa: B018, the property reads the programs
