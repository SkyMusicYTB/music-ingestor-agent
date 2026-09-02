from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import SplitResult, urlsplit

from app.sources.identities import ProviderIdentity, ProviderUse
from app.sources.models import EvidenceReference, SourceCandidate, SourcePolicy
from app.sources.providers import provider_capability, provider_for_extractor


class SourcePolicyViolation(ValueError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class PolicyValidation:
    allowed: bool
    reason_code: str
    requires_resolution: bool = False


@runtime_checkable
class URLPolicyValidator(Protocol):
    def validate(self, url: str, *, provider: ProviderIdentity) -> PolicyValidation: ...


@runtime_checkable
class NetworkPolicyValidator(Protocol):
    def validate_hostname(self, hostname: str) -> PolicyValidation: ...

    def validate_resolved_addresses(self, addresses: Iterable[str]) -> PolicyValidation: ...


@runtime_checkable
class ProviderPolicyValidator(Protocol):
    def validate(self, provider: ProviderIdentity, *, use: ProviderUse) -> PolicyValidation: ...


class RegistryProviderPolicy:
    def validate(self, provider: ProviderIdentity, *, use: ProviderUse) -> PolicyValidation:
        capability = provider_capability(provider)
        if capability.supports(use):
            return PolicyValidation(True, "provider_allowed")
        return PolicyValidation(False, f"provider_not_allowed_for_{use.value}")


class PublicNetworkPolicy:
    """Pure address policy. Resolution remains the transport boundary's responsibility."""

    _BLOCKED_HOSTS = frozenset({"localhost", "localhost.localdomain"})

    def validate_hostname(self, hostname: str) -> PolicyValidation:
        normalized = hostname.rstrip(".").casefold()
        if not normalized or normalized in self._BLOCKED_HOSTS or normalized.endswith(".localhost"):
            return PolicyValidation(False, "network_host_blocked")
        try:
            address = ipaddress.ip_address(normalized)
        except ValueError:
            return PolicyValidation(True, "network_resolution_required", requires_resolution=True)
        if not _is_public_address(address):
            return PolicyValidation(False, "network_address_not_public")
        return PolicyValidation(True, "network_address_public")

    def validate_resolved_addresses(self, addresses: Iterable[str]) -> PolicyValidation:
        parsed: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for value in addresses:
            try:
                parsed.append(ipaddress.ip_address(value))
            except ValueError:
                return PolicyValidation(False, "network_resolution_invalid")
        if not parsed:
            return PolicyValidation(False, "network_resolution_empty")
        if any(not _is_public_address(address) for address in parsed):
            return PolicyValidation(False, "network_resolution_not_public")
        return PolicyValidation(True, "network_resolution_public")


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Apply the complete egress denylist instead of relying on ``is_global`` alone."""

    return bool(
        address.is_global
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_reserved
        and not address.is_private
        and not (isinstance(address, ipaddress.IPv6Address) and address.is_site_local)
    )


class ProviderURLPolicy:
    def __init__(self, network_policy: NetworkPolicyValidator | None = None) -> None:
        self._network = network_policy or PublicNetworkPolicy()

    def validate(self, url: str, *, provider: ProviderIdentity) -> PolicyValidation:
        if (
            not isinstance(url, str)
            or not url
            or len(url) > 2_048
            or "\\" in url
            or any(ord(character) < 32 or ord(character) == 127 for character in url)
        ):
            return PolicyValidation(False, "url_invalid")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            return PolicyValidation(False, "url_invalid")
        structural = self._validate_structure(parsed, port)
        if structural is not None:
            return structural
        hostname = parsed.hostname
        if hostname is None:
            return PolicyValidation(False, "url_host_missing")
        capability = provider_capability(provider)
        if not capability.accepts_hostname(hostname):
            return PolicyValidation(False, "url_provider_host_mismatch")
        network = self._network.validate_hostname(hostname)
        if not network.allowed:
            return network
        return PolicyValidation(
            True,
            "url_allowed",
            requires_resolution=network.requires_resolution,
        )

    @staticmethod
    def _validate_structure(parsed: SplitResult, port: int | None) -> PolicyValidation | None:
        if parsed.scheme.casefold() != "https":
            return PolicyValidation(False, "url_scheme_not_https")
        if parsed.username is not None or parsed.password is not None:
            return PolicyValidation(False, "url_credentials_forbidden")
        if parsed.fragment:
            return PolicyValidation(False, "url_fragment_forbidden")
        if port not in {None, 443}:
            return PolicyValidation(False, "url_port_forbidden")
        if not parsed.netloc or parsed.hostname is None:
            return PolicyValidation(False, "url_host_missing")
        return None


def validate_provider_use(
    provider: ProviderIdentity,
    *,
    use: ProviderUse,
    validator: ProviderPolicyValidator | None = None,
) -> PolicyValidation:
    return (validator or RegistryProviderPolicy()).validate(provider, use=use)


def validate_source_candidate(
    candidate: SourceCandidate,
    policy: SourcePolicy,
    *,
    url_validator: URLPolicyValidator | None = None,
    provider_validator: ProviderPolicyValidator | None = None,
) -> tuple[PolicyValidation, ...]:
    checks: list[PolicyValidation] = []
    if candidate.provider not in policy.allowed_providers:
        checks.append(PolicyValidation(False, "provider_disabled_by_policy"))
    checks.append(
        (provider_validator or RegistryProviderPolicy()).validate(
            candidate.provider,
            use=ProviderUse.ACQUISITION,
        )
    )
    extractor_provider = provider_for_extractor(candidate.extractor)
    checks.append(
        PolicyValidation(
            extractor_provider is candidate.provider,
            (
                "extractor_provider_match"
                if extractor_provider is candidate.provider
                else "extractor_provider_mismatch"
            ),
        )
    )
    checks.append(
        (url_validator or ProviderURLPolicy()).validate(
            candidate.url,
            provider=candidate.provider,
        )
    )
    return tuple(checks)


def validate_evidence_reference(
    reference: EvidenceReference,
    *,
    url_validator: URLPolicyValidator | None = None,
    provider_validator: ProviderPolicyValidator | None = None,
) -> tuple[PolicyValidation, ...]:
    checks = [
        (provider_validator or RegistryProviderPolicy()).validate(
            reference.provider,
            use=ProviderUse.EVIDENCE,
        )
    ]
    if reference.url is not None:
        checks.append(
            (url_validator or ProviderURLPolicy()).validate(
                reference.url,
                provider=reference.provider,
            )
        )
    return tuple(checks)


def require_source_candidate(
    candidate: SourceCandidate,
    policy: SourcePolicy,
    *,
    url_validator: URLPolicyValidator | None = None,
    provider_validator: ProviderPolicyValidator | None = None,
) -> tuple[PolicyValidation, ...]:
    checks = validate_source_candidate(
        candidate,
        policy,
        url_validator=url_validator,
        provider_validator=provider_validator,
    )
    violation = next((check for check in checks if not check.allowed), None)
    if violation is not None:
        raise SourcePolicyViolation(violation.reason_code)
    return checks


def require_evidence_reference(
    reference: EvidenceReference,
    *,
    url_validator: URLPolicyValidator | None = None,
    provider_validator: ProviderPolicyValidator | None = None,
) -> tuple[PolicyValidation, ...]:
    checks = validate_evidence_reference(
        reference,
        url_validator=url_validator,
        provider_validator=provider_validator,
    )
    violation = next((check for check in checks if not check.allowed), None)
    if violation is not None:
        raise SourcePolicyViolation(violation.reason_code)
    return checks
