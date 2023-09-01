# Copyright (c) 2023 - 2023, Oracle and/or its affiliates. All rights reserved.
# Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl/.


"""This module contains data related to one package registry that is matched against a repository."""

from dataclasses import dataclass, field

from macaron.slsa_analyzer.build_tool import BaseBuildTool
from macaron.slsa_analyzer.package_registry import PackageRegistry
from macaron.slsa_analyzer.provenance.provenance import DownloadedProvenanceData


@dataclass
class PackageRegistryInfo:
    """This class contains data for one package registry that is matched against a repository."""

    #: The build tool matched against the repository.
    build_tool: BaseBuildTool
    #: The package registry matched against the repository. This is dependent on the build tool detected.
    package_registry: PackageRegistry
    #: The provenances matched against the current repo.
    provenances: list[DownloadedProvenanceData] = field(default_factory=list)
