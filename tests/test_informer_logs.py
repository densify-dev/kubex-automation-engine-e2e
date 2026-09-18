from helpers import InformerStart, informer_structured_allowed, parse_informer_start_logs


def test_parse_json_informer_record():
    records, errors = parse_informer_start_logs(
        '2026-01-01T00:00:00Z INFO informer starting informer {"kind":"CronJob","gvk":"batch/v1, Kind=CronJob","cache_mode":"metadata"}'
    )
    assert errors == []
    assert records == [InformerStart("CronJob", "batch/v1, Kind=CronJob", "metadata")]


def test_parse_console_record_and_reject_malformed_record():
    records, errors = parse_informer_start_logs(
        'starting informer {"kind":"Pod","gvk":"/v1, Kind=Pod","cache_mode":"structured"}\n'
        'starting informer {"kind":"ConfigMap"}'
    )
    assert records == [InformerStart("Pod", "/v1, Kind=Pod", "structured")]
    assert len(errors) == 1


def test_parse_invalid_escape_reports_malformed_record():
    records, errors = parse_informer_start_logs(
        r'starting informer {"kind":"Pod","gvk":"/v1, Kind=Pod","cache_mode":"structured\q"}'
    )
    assert records == []
    assert len(errors) == 1


def test_parse_concatenated_json_objects_does_not_splice_fields():
    """PD-60602: two log entries concatenated onto one line (no separator)
    must never be spliced into a fabricated record combining fields from
    both -- that previously misread a webhook validation log next to an
    informer-start log as a forbidden structured Deployment informer."""
    records, errors = parse_informer_start_logs(
        'starting informer {"kind":"AutomationStrategy","name":"x"}'
        '{"gvk":"apps/v1, Kind=Deployment","cache_mode":"structured"}'
    )
    assert records == []
    assert len(errors) == 1


def test_structured_allowlist_matches_pd60602():
    assert informer_structured_allowed(InformerStart("Pod", "/v1, Kind=Pod", "structured"))
    assert informer_structured_allowed(InformerStart("Node", "/v1, Kind=Node", "structured"))
    assert informer_structured_allowed(InformerStart("Policy", "rightsizing.kubex.ai/v1alpha1, Kind=Policy", "structured"))
    assert not informer_structured_allowed(InformerStart("ConfigMap", "/v1, Kind=ConfigMap", "structured"))
