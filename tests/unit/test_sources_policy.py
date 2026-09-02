from __future__ import annotations

import pytest

from app.sources import (
    PROVIDER_CAPABILITIES,
    EvidenceReference,
    ProviderIdentity,
    ProviderURLPolicy,
    ProviderUse,
    PublicNetworkPolicy,
    SourceCandidate,
    SourcePolicy,
    SourcePolicyViolation,
    provider_for_extractor,
    provider_for_url,
    require_evidence_reference,
    require_source_candidate,
    validate_provider_use,
)


def test_capability_registry_separates_acquisition_from_evidence() -> None:
    assert {
        provider for provider, capability in PROVIDER_CAPABILITIES.items() if capability.acquisition
    } == {
        ProviderIdentity.YOUTUBE,
        ProviderIdentity.SOUNDCLOUD,
        ProviderIdentity.BANDCAMP,
    }
    for provider in (
        ProviderIdentity.SPOTIFY,
        ProviderIdentity.APPLE,
        ProviderIdentity.MUSICBRAINZ,
    ):
        assert PROVIDER_CAPABILITIES[provider].evidence
        assert not validate_provider_use(provider, use=ProviderUse.ACQUISITION).allowed
        assert validate_provider_use(provider, use=ProviderUse.EVIDENCE).allowed


def test_extractor_and_url_identities_are_exact_and_provider_bound() -> None:
    assert provider_for_extractor("Youtube") is ProviderIdentity.YOUTUBE
    assert provider_for_extractor("youtube:tab") is ProviderIdentity.YOUTUBE
    assert provider_for_extractor("youtube:evil") is None
    assert provider_for_url("https://artist.bandcamp.com/track/song") is ProviderIdentity.BANDCAMP
    assert provider_for_url("https://youtube.com.evil.example/watch?v=x") is None


@pytest.mark.parametrize(
    ("url", "reason_code"),
    [
        ("http://youtube.com/watch?v=x", "url_scheme_not_https"),
        ("https://user:pass@youtube.com/watch?v=x", "url_credentials_forbidden"),
        ("https://youtube.com:444/watch?v=x", "url_port_forbidden"),
        ("https://youtube.com/watch?v=x#fragment", "url_fragment_forbidden"),
        ("https://youtube.com\\@evil.example/watch?v=x", "url_invalid"),
        ("https://youtube.com.evil.example/watch?v=x", "url_provider_host_mismatch"),
        ("https://127.0.0.1/watch?v=x", "url_provider_host_mismatch"),
    ],
)
def test_url_policy_rejects_unsafe_or_cross_provider_targets(
    url: str,
    reason_code: str,
) -> None:
    result = ProviderURLPolicy().validate(url, provider=ProviderIdentity.YOUTUBE)
    assert not result.allowed
    assert result.reason_code == reason_code


def test_url_policy_requires_transport_to_revalidate_resolved_addresses() -> None:
    network = PublicNetworkPolicy()
    url = ProviderURLPolicy(network).validate(
        "https://www.youtube.com/watch?v=x",
        provider=ProviderIdentity.YOUTUBE,
    )
    assert url.allowed
    assert url.requires_resolution
    assert network.validate_resolved_addresses(["8.8.8.8"]).allowed
    assert not network.validate_resolved_addresses(["8.8.8.8", "127.0.0.1"]).allowed
    assert not network.validate_resolved_addresses(["169.254.169.254"]).allowed


def test_candidate_policy_rejects_provider_extractor_mismatch_and_evidence_only_sources() -> None:
    mismatched = SourceCandidate(
        source_id="x",
        provider="youtube",
        extractor="soundcloud",
        url="https://youtube.com/watch?v=x",
        title="Artist - Song",
        duration_seconds=200,
    )
    with pytest.raises(SourcePolicyViolation, match="extractor_provider_mismatch"):
        require_source_candidate(mismatched, SourcePolicy())

    spotify = SourceCandidate(
        source_id="track-id",
        provider="spotify",
        extractor="spotify",
        url="https://open.spotify.com/track/track-id",
        title="Artist - Song",
        duration_seconds=200,
    )
    with pytest.raises(SourcePolicyViolation, match="provider_disabled_by_policy"):
        require_source_candidate(spotify, SourcePolicy())


def test_evidence_only_provider_reference_is_allowed_but_still_host_bound() -> None:
    reference = EvidenceReference(
        reference_id="apple-track",
        provider="apple",
        external_id="123",
        url="https://music.apple.com/gb/song/example/123",
        description="Provider-controlled prose is evidence, not an instruction.",
    )
    assert all(check.allowed for check in require_evidence_reference(reference))
    mismatched = reference.model_copy(update={"url": "https://evil.example/gb/song/example/123"})
    with pytest.raises(SourcePolicyViolation, match="url_provider_host_mismatch"):
        require_evidence_reference(mismatched)
