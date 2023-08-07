# Copyright (c) 2022 - 2023, Oracle and/or its affiliates. All rights reserved.
# Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl/.

"""This module contains the implementation of the Provenance Available check."""

import logging
import os
import tempfile
from collections.abc import Sequence
from types import SimpleNamespace
from typing import cast

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.sqltypes import String

from macaron.config.defaults import defaults
from macaron.database.table_definitions import CheckFacts
from macaron.errors import MacaronError, ProvenanceLoadError
from macaron.slsa_analyzer.analyze_context import AnalyzeContext
from macaron.slsa_analyzer.asset import IsAsset
from macaron.slsa_analyzer.build_tool.gradle import Gradle
from macaron.slsa_analyzer.checks.base_check import BaseCheck
from macaron.slsa_analyzer.checks.check_result import CheckResult, CheckResultType
from macaron.slsa_analyzer.ci_service.base_ci_service import NoneCIService
from macaron.slsa_analyzer.package_registry import JFrogMavenRegistry
from macaron.slsa_analyzer.package_registry.jfrog_maven_registry import JFrogMavenAsset
from macaron.slsa_analyzer.provenance.loader import ProvPayloadLoader, SLSAProvenanceError, load_provenance
from macaron.slsa_analyzer.provenance.witness import (
    WitnessProvenance,
    extract_repo_url,
    is_witness_provenance_payload,
    load_witness_verifier_config,
)
from macaron.slsa_analyzer.registry import registry
from macaron.slsa_analyzer.slsa_req import ReqName
from macaron.slsa_analyzer.specs.ci_spec import CIInfo
from macaron.slsa_analyzer.specs.package_registry_spec import PackageRegistryInfo
from macaron.util import JsonType

logger: logging.Logger = logging.getLogger(__name__)


class ProvenanceAvailableException(MacaronError):
    """When there is an error while checking if a provenance is available."""


class ProvenanceAvailableFacts(CheckFacts):
    """The ORM mapping for justifications in provenance_available check."""

    __tablename__ = "_provenance_available_check"

    #: The primary key.
    id: Mapped[int] = mapped_column(ForeignKey("_check_facts.id"), primary_key=True)  # noqa: A003

    #: The provenance asset name.
    asset_name: Mapped[str] = mapped_column(String, nullable=False)

    #: The URL for the provenance asset.
    asset_url: Mapped[str] = mapped_column(String, nullable=True)

    __mapper_args__ = {
        "polymorphic_identity": "_provenance_available_check",
    }


class ProvenanceAvailableCheck(BaseCheck):
    """This Check checks whether the target repo has in-toto provenance."""

    def __init__(self) -> None:
        """Initialize instance."""
        check_id = "mcn_provenance_available_1"
        description = "Check whether the target has intoto provenance."
        depends_on: list[tuple[str, CheckResultType]] = []
        eval_reqs = [
            ReqName.PROV_AVAILABLE,
            ReqName.PROV_CONT_BUILD_INS,
            ReqName.PROV_CONT_ARTI,
            ReqName.PROV_CONT_BUILDER,
        ]
        super().__init__(check_id=check_id, description=description, depends_on=depends_on, eval_reqs=eval_reqs)

    def find_provenance_assets_on_package_registries(
        self,
        repo_fs_path: str,
        repo_remote_path: str,
        package_registry_info_entries: list[PackageRegistryInfo],
        provenance_extensions: list[str],
    ) -> Sequence[IsAsset]:
        """Find provenance assets on package registries.

        Note that we stop going through package registries once we encounter a package
        registry that does host provenance assets.

        Parameters
        ----------
        repo_fs_path : str
            The path to the repo on the local file system.
        repo_remote_path : str
            The URL to the remote repository.
        package_registry_info_entries : list[PackageRegistryInfo]
            A list of package registry info entries.
        provenance_extensions : list[str]
            A list of provenance extensions. Assets with these extensions are assumed
            to be provenances.

        Returns
        -------
        Sequence[IsAsset]
            A sequence of provenance assets found on one of the package registries.
            This sequence is empty if there is no provenance assets found.

        Raises
        ------
        ProvenanceAvailableException
            If there is an error finding provenance assets that should result in failing
            the check altogether.
        """
        for package_registry_info_entry in package_registry_info_entries:
            match package_registry_info_entry:
                case PackageRegistryInfo(
                    build_tool=Gradle() as gradle,
                    package_registry=JFrogMavenRegistry() as jfrog_registry,
                ) as info_entry:
                    # Triples of group id, artifact id, version.
                    gavs: list[tuple[str, str, str]] = []

                    group_ids = gradle.get_group_ids(repo_fs_path)
                    for group_id in group_ids:
                        artifact_ids = jfrog_registry.fetch_artifact_ids(group_id)

                        for artifact_id in artifact_ids:
                            latest_version = jfrog_registry.fetch_latest_version(
                                group_id,
                                artifact_id,
                            )
                            if not latest_version:
                                continue
                            logger.info(
                                "Found the latest version %s for Maven package %s:%s",
                                latest_version,
                                group_id,
                                artifact_id,
                            )
                            gavs.append((group_id, artifact_id, latest_version))

                    provenance_assets = []
                    for group_id, artifact_id, version in gavs:
                        provenance_assets.extend(
                            jfrog_registry.fetch_assets(
                                group_id=group_id,
                                artifact_id=artifact_id,
                                version=version,
                                extensions=set(provenance_extensions),
                            )
                        )

                    if not provenance_assets:
                        continue

                    # We check the size of the provenance against a max valid size.
                    # This is a prevention against malicious denial-of-service attacks when an
                    # adversary provides a super large malicious file.

                    # TODO: refactor the size checking in this check and the `provenance_l3_check`
                    # so that we have consistent behavior when checking provenance size.
                    # The schema of the ini config also needs changing.
                    max_valid_provenance_size = defaults.getint(
                        "slsa.verifier",
                        "max_download_size",
                        fallback=1000000,
                    )

                    for provenance_asset in provenance_assets:
                        if provenance_asset.size_in_bytes > max_valid_provenance_size:
                            msg = (
                                f"The provenance asset {provenance_asset.name} unexpectedly exceeds the "
                                f"max valid file size of {max_valid_provenance_size} (bytes). "
                                "The check will not proceed due to potential security risks."
                            )
                            logger.error(msg)
                            raise ProvenanceAvailableException(msg)

                    logger.info("Found the following provenance assets:")

                    provenances = self.obtain_witness_provenances(
                        provenance_assets=provenance_assets,
                        repo_remote_path=repo_remote_path,
                    )

                    witness_provenance_assets = []
                    for provenance in provenances:
                        logger.info("* %s", provenance.asset.url)
                        witness_provenance_assets.append(provenance.asset)

                    # Persist the provenance assets in the package registry info entry.
                    info_entry.provenances.extend(provenances)
                    return provenance_assets

        return []

    def obtain_witness_provenances(
        self,
        provenance_assets: Sequence[IsAsset],
        repo_remote_path: str,
    ) -> list[WitnessProvenance]:
        """Obtain the witness provenances produced from a repository.

        Parameters
        ----------
        provenance_assets : Sequence[Asset]
            A list of provenance assets, some of which can be witness provenances.
        repo_remote_path : str
            The remote path of the repo being analyzed.

        Returns
        -------
        list[WitnessProvenance]
            A list of witness provenances that are produced by the repo being analyzed.
        """
        provenances = []
        witness_verifier_config = load_witness_verifier_config()

        with tempfile.TemporaryDirectory() as temp_dir:
            for provenance_asset in provenance_assets:
                provenance_filepath = os.path.join(temp_dir, provenance_asset.name)
                if not provenance_asset.download(provenance_filepath):
                    logger.debug(
                        "Could not download the provenance %s. Skip verifying...",
                        provenance_asset.name,
                    )
                    continue

                try:
                    provenance_payload = load_provenance(
                        provenance_filepath,
                    )
                except ProvenanceLoadError as error:
                    logger.error("Error while loading provenance: %s", error)
                    continue

                if not is_witness_provenance_payload(
                    provenance_payload,
                    witness_verifier_config.predicate_types,
                ):
                    continue

                repo_url = extract_repo_url(provenance_payload)
                if not repo_url != repo_remote_path:
                    continue

                provenances.append(
                    WitnessProvenance(
                        asset=provenance_asset,
                        payload=provenance_payload,
                    )
                )

        return provenances

    def download_provenances_from_jfrog_maven_package_registry(
        self,
        download_dir: str,
        provenance_assets: list[JFrogMavenAsset],
        jfrog_maven_registry: JFrogMavenRegistry,
    ) -> dict[str, dict[str, JsonType]]:
        """Download provenances from a JFrog Maven package registry.

        Parameters
        ----------
        download_dir : str
            The directory where provenance assets are downloaded to.
        provenance_assets : list[JFrogMavenAsset]
            The list of provenance assets.
        jfrog_maven_registry : JFrogMavenRegistry
            The JFrog Maven registry instance.

        Returns
        -------
        dict[str, dict[str, JsonType]]
            The downloaded provenance payloads. Each key is the URL where the provenance
            asset is hosted and each value is the corresponding provenance payload in JSON.
        """
        # Note: In certain cases, Macaron can find the same provenance file in
        # multiple different places on a package registry.
        #
        # We may consider de-duplicating this file, so that we do not run the same
        # steps on the same file multiple times.

        # Download the provenance assets and load them into dictionaries.
        provenances = {}

        for prov_asset in provenance_assets:
            provenance_filepath = os.path.join(download_dir, prov_asset.name)
            if not jfrog_maven_registry.download_asset(prov_asset.url, provenance_filepath):
                logger.debug(
                    "Could not download the provenance %s. Skip verifying...",
                    prov_asset.name,
                )
                continue

            try:
                provenances[prov_asset.url] = load_provenance(
                    provenance_filepath,
                )
            except ProvenanceLoadError as error:
                logger.error("Error while loading provenance: %s", error)
                continue

        return provenances

    def find_provenance_assets_on_ci_services(
        self,
        repo_full_name: str,
        ci_info_entries: list[CIInfo],
        provenance_extensions: list[str],
    ) -> Sequence[IsAsset]:
        """Find provenance assets on CI services.

        Note that we stop going through the CI services once we encounter a CI service
        that does host provenance assets.

        This method also loads the provenance payloads into the ``CIInfo`` object where
        the provenance assets are found.

        Parameters
        ----------
        repo_full_name: str
            The full name of the repo, in the format of ``owner/repo_name``.
        package_registry_info_entries : list[PackageRegistryInfo]
            A list of package registry info entries.
        provenance_extensions : list[str]
            A list of provenance extensions. Assets with these extensions are assumed
            to be provenances.

        Returns
        -------
        Sequence[Asset]
            A sequence of assets found on the given CI services.
        """
        for ci_info in ci_info_entries:
            ci_service = ci_info["service"]

            if isinstance(ci_service, NoneCIService):
                continue

            # Only get the latest release.
            release = ci_service.api_client.get_latest_release(repo_full_name)
            if not release:
                logger.info("Did not find any release on %s.", ci_service.name)
                continue

            # Store the release data for other checks.
            ci_info["latest_release"] = release

            # Get the provenance assets.
            for prov_ext in provenance_extensions:
                provenance_assets = ci_service.api_client.get_assets(release, ext=prov_ext)
                if not provenance_assets:
                    continue

                logger.info("Found the following provenance assets:")
                for provenance_asset in provenance_assets:
                    logger.info("* %s", provenance_asset["url"])

                # Store the provenance assets for other checks.
                ci_info["provenance_assets"].extend(provenance_assets)

                # Download the provenance assets and load the provenance payloads.
                self.download_provenances_from_github_actions_ci_service(
                    ci_info,
                )

                return [
                    cast(IsAsset, SimpleNamespace(**provenance_asset))
                    for provenance_asset in ci_info["provenance_assets"]
                ]

        return []

    def download_provenances_from_github_actions_ci_service(self, ci_info: CIInfo) -> None:
        """Download provenances from GitHub Actions.

        Parameters
        ----------
        ci_info: CIInfo,
            A ``CIInfo`` instance that holds a GitHub Actions git service object.
        """
        ci_service = ci_info["service"]
        prov_assets = ci_info["provenance_assets"]

        with tempfile.TemporaryDirectory() as temp_path:
            downloaded_provs = []
            for prov_asset in prov_assets:
                # Check the size before downloading.
                if int(prov_asset["size"]) > defaults.getint(
                    "slsa.verifier",
                    "max_download_size",
                    fallback=1000000,
                ):
                    logger.info(
                        "Skip verifying the provenance %s: asset size too large.",
                        prov_asset["name"],
                    )
                    continue

                provenance_filepath = os.path.join(temp_path, prov_asset["name"])

                if not ci_service.api_client.download_asset(
                    prov_asset["url"],
                    provenance_filepath,
                ):
                    logger.debug(
                        "Could not download the provenance %s. Skip verifying...",
                        prov_asset["name"],
                    )
                    continue

                # Read the provenance.
                try:
                    payload = ProvPayloadLoader.load(provenance_filepath)
                except SLSAProvenanceError as error:
                    logger.error("Error logging provenance: %s", error)
                    continue

                # Add the provenance file.
                downloaded_provs.append(payload)

            # Persist the provenance payloads into the CIInfo object.
            ci_info["provenances"] = downloaded_provs

    def run_check(self, ctx: AnalyzeContext, check_result: CheckResult) -> CheckResultType:
        """Implement the check in this method.

        Parameters
        ----------
        ctx : AnalyzeContext
            The object containing processed data for the target repo.
        check_result : CheckResult
            The object containing result data of a check.

        Returns
        -------
        CheckResultType
            The result type of the check (e.g. PASSED).
        """
        provenance_extensions = defaults.get_list(
            "slsa.verifier",
            "provenance_extensions",
            fallback=["intoto.jsonl"],
        )

        # We look for the provenances in the package registries first, then CI services.
        # (Note the short-circuit evaluation with OR.)
        try:
            provenance_assets = self.find_provenance_assets_on_package_registries(
                repo_fs_path=ctx.component.repository.fs_path,
                repo_remote_path=ctx.component.repository.remote_path,
                package_registry_info_entries=ctx.dynamic_data["package_registries"],
                provenance_extensions=provenance_extensions,
            ) or self.find_provenance_assets_on_ci_services(
                repo_full_name=ctx.component.repository.full_name,
                ci_info_entries=ctx.dynamic_data["ci_services"],
                provenance_extensions=provenance_extensions,
            )
        except ProvenanceAvailableException as error:
            check_result["justification"] = [str(error)]
            return CheckResultType.FAILED

        if provenance_assets:
            ctx.dynamic_data["is_inferred_prov"] = False

            check_result["justification"].append("Found provenance in release assets:")
            check_result["justification"].extend(
                [asset.name for asset in provenance_assets],
            )
            # We only write the result to the database when the check is PASSED.
            check_result["result_tables"] = [
                ProvenanceAvailableFacts(
                    asset_name=asset.name,
                    asset_url=asset.url,
                )
                for asset in provenance_assets
            ]
            return CheckResultType.PASSED

        check_result["justification"].append("Could not find any SLSA provenances.")
        return CheckResultType.FAILED


registry.register(ProvenanceAvailableCheck())
