from pathlib import Path


from alice.pipeline import alice_eval_dataset as ds


def test_scope_regression_dataset_has_required_failure_modes():
    cases = ds.load_cases()
    ids = {case["id"] for case in cases}

    assert "scope_mcp_docs_audit" in ids
    assert "paste_chunk_wait" in ids
    assert "observability_stack_question" in ids
    assert "layer_stack_architecture_question" in ids


def test_phoenix_examples_preserve_expected_outputs():
    examples = ds.phoenix_examples(ds.load_cases())
    first = examples[0]

    assert first["input"]["user_text"]
    assert first["output"]["classification"] in {"in_scope", "buffer_paste"}
    assert isinstance(first["output"]["must_not_include"], list)
    assert first["metadata"]["id"]
