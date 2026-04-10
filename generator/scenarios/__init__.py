"""Named scenario YAML configurations for the ATO Reasoner event generator.

Each YAML file in this package defines one named scenario and is validated
against generator.config.ScenarioConfig at load time. The filename stem must
match the scenario_id field in the YAML.

Scenarios:
    baseline_normal           — Normal user, stable device/geo. Expected: ALLOW
    credential_stuffing_burst — High-velocity multi-account attack. Expected: BLOCK
    post_breach_ato           — Valid creds, new device + geo. Expected: HOLD or BLOCK
    high_velocity_legitimate  — Legitimate API client, high rate.
                                Expected: ALLOW or CHALLENGE
    geo_impossible            — Physically impossible travel speed. Expected: BLOCK
    device_fingerprint_anomaly — Partial fingerprint match. Expected: CHALLENGE
    novel_entity              — New account, sparse history. Expected: CHALLENGE or HOLD
    adversarial_probe         — Injection + schema violation attempt. Expected: BLOCK
"""
