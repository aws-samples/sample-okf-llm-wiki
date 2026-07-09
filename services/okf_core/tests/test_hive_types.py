from okf_core.hive_types import flatten_hive_type


def test_scalar():
    fields = flatten_hive_type("raceid", "bigint")
    assert len(fields) == 1
    assert (
        fields[0].name == "raceid"
        and fields[0].type == "bigint"
        and fields[0].depth == 0
    )


def test_decimal_preserved():
    fields = flatten_hive_type("amount", "decimal(10,2)")
    assert fields[0].type == "decimal(10,2)"


def test_struct_flattens_children():
    fields = flatten_hive_type("author", "struct<login:string,id:bigint>")
    names = {f.name: f.type for f in fields}
    assert names["author"] == "struct"
    assert names["author.login"] == "string"
    assert names["author.id"] == "bigint"
    # children are one level deeper
    child = next(f for f in fields if f.name == "author.login")
    assert child.depth == 1


def test_array_of_scalar():
    fields = flatten_hive_type("tags", "array<string>")
    assert len(fields) == 1
    assert fields[0].type == "array<string>"


def test_array_of_struct():
    fields = flatten_hive_type("commits", "array<struct<sha:string,msg:string>>")
    names = {f.name: f.type for f in fields}
    assert names["commits"] == "array<struct>"
    assert names["commits.sha"] == "string"
    assert names["commits.msg"] == "string"


def test_nested_struct():
    fields = flatten_hive_type("a", "struct<b:struct<c:int>>")
    names = {f.name: f.type for f in fields}
    assert names["a"] == "struct"
    assert names["a.b"] == "struct"
    assert names["a.b.c"] == "int"
    assert next(f for f in fields if f.name == "a.b.c").depth == 2


def test_map_preserved():
    fields = flatten_hive_type("props", "map<string,bigint>")
    assert fields[0].type == "map<string,bigint>"
