# Copyright (c) 2022 - 2023, Oracle and/or its affiliates. All rights reserved.
# Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl/.

"""This module contains the BuildAsCodeCheck class."""

import logging
import os

from problog import get_evaluatable
from problog.program import PrologString
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.sqltypes import Float, String

from macaron.config.defaults import defaults
from macaron.database.database_manager import ORMBase
from macaron.database.table_definitions import CheckFactsTable
from macaron.slsa_analyzer.analyze_context import AnalyzeContext
from macaron.slsa_analyzer.build_tool.base_build_tool import BaseBuildTool, NoneBuildTool
from macaron.slsa_analyzer.build_tool.pip import Pip
from macaron.slsa_analyzer.checks import bac_
from macaron.slsa_analyzer.checks.base_check import BaseCheck
from macaron.slsa_analyzer.checks.check_result import CheckResult, CheckResultType
from macaron.slsa_analyzer.ci_service.base_ci_service import BaseCIService, NoneCIService
from macaron.slsa_analyzer.ci_service.circleci import CircleCI
from macaron.slsa_analyzer.ci_service.github_actions import GHWorkflowType
from macaron.slsa_analyzer.ci_service.gitlab_ci import GitLabCI
from macaron.slsa_analyzer.ci_service.jenkins import Jenkins
from macaron.slsa_analyzer.ci_service.travis import Travis
from macaron.slsa_analyzer.registry import registry
from macaron.slsa_analyzer.slsa_req import ReqName
from macaron.slsa_analyzer.specs.ci_spec import CIInfo

logger: logging.Logger = logging.getLogger(__name__)


class BuildAsCodeTable(CheckFactsTable, ORMBase):
    """Check justification table for build_as_code."""

    __tablename__ = "_build_as_code_check"
    build_tool_name: Mapped[str] = mapped_column(String, nullable=True)
    ci_service_name: Mapped[str] = mapped_column(String, nullable=True)
    build_trigger: Mapped[str] = mapped_column(String, nullable=True)
    deploy_command: Mapped[str] = mapped_column(String, nullable=True)
    build_status_url: Mapped[str] = mapped_column(String, nullable=True)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=True)


def has_deploy_command(commands: list[list[str]], build_tool: BaseBuildTool) -> str:
    """Check if the bash command is a build and deploy command."""
    # Account for Python projects having separate tools for packaging and publishing.
    deploy_tool = build_tool.publisher if build_tool.publisher else build_tool.builder
    for com in commands:

        # Check for empty or invalid commands.
        if not com or not com[0]:
            continue
        # The first argument in a bash command is the program name.
        # So first check that the program name is a supported build tool name.
        # We need to handle cases where the first argument is a path to the program.
        cmd_program_name = os.path.basename(com[0])
        if not cmd_program_name:
            logger.debug("Found invalid program name %s.", com[0])
            continue

        check_build_commands = any(build_cmd for build_cmd in deploy_tool if build_cmd == cmd_program_name)

        # Support the use of interpreters like Python that load modules, i.e., 'python -m pip install'.
        check_module_build_commands = any(
            interpreter == cmd_program_name
            and com[1]
            and com[1] in build_tool.interpreter_flag
            and com[2]
            and com[2] in deploy_tool
            for interpreter in build_tool.interpreter
        )
        prog_name_index = 2 if check_module_build_commands else 0

        if check_build_commands or check_module_build_commands:
            # Check the arguments in the bash command for the deploy goals.
            # If there are no deploy args for this build tool, accept as deploy command.
            if not build_tool.deploy_arg:
                logger.info("No deploy arguments required. Accept %s as deploy command.", str(com))
                return str(com)

            for word in com[(prog_name_index + 1) :]:
                # TODO: allow plugin versions in arguments, e.g., maven-plugin:1.6.8:deploy.
                if word in build_tool.deploy_arg:
                    logger.info("Found deploy command %s.", str(com))
                    return str(com)
    return ""


def ci_parsed_subcheck(ci_info: CIInfo) -> dict:
    """Check whether parsing is supported for this CI service's CI config files."""
    check_certainty = 1

    justification: list[str | dict[str, str]] = ["The CI workflow files for this CI service are parsed."]

    if ci_info["bash_commands"]:
        return {"certainty": check_certainty, "justification": justification}
    return {"certainty": 0, "justification": [{"The CI workflow files for this CI service aren't parsed."}]}


def deploy_action_subcheck(
    ctx: AnalyzeContext, ci_info: CIInfo, ci_service: BaseCIService, build_tool: BaseBuildTool
) -> dict:
    """Check for use of a trusted Github Actions workflow to publish/deploy."""
    # TODO: verify that deployment is legitimate and not a test
    check_certainty = 0.8

    if isinstance(build_tool, Pip):
        trusted_deploy_actions = defaults.get_list("builder.pip.ci.deploy", "github_actions", fallback=[])

        for callee in ci_info["callgraph"].bfs():
            workflow_name = callee.name.split("@")[0]

            if not workflow_name or callee.node_type not in [
                GHWorkflowType.EXTERNAL,
                GHWorkflowType.REUSABLE,
            ]:
                logger.debug("Workflow %s is not relevant. Skipping...", callee.name)
                continue
            if workflow_name in trusted_deploy_actions:
                trigger_link = ci_service.api_client.get_file_link(
                    ctx.repo_full_name,
                    ctx.commit_sha,
                    ci_service.api_client.get_relative_path_of_workflow(os.path.basename(callee.caller_path)),
                )
                deploy_action_source_link = ci_service.api_client.get_file_link(
                    ctx.repo_full_name, ctx.commit_sha, callee.caller_path
                )

                html_url = ci_service.has_latest_run_passed(
                    ctx.repo_full_name,
                    ctx.branch_name,
                    ctx.commit_sha,
                    ctx.commit_date,
                    os.path.basename(callee.caller_path),
                )

                # TODO: include in the justification multiple cases of external action usage
                justification: list[str | dict[str, str]] = [
                    {
                        "To deploy": deploy_action_source_link,
                        "The build is triggered by": trigger_link,
                    },
                    f"Deploy action: {workflow_name}",
                    {"The status of the build can be seen at": html_url}
                    if html_url
                    else "However, could not find a passing workflow run.",
                ]

                return {
                    "certainty": check_certainty,
                    "justification": justification,
                    "deploy_command": workflow_name,
                    "trigger_link": trigger_link,
                    "deploy_action_source_link": deploy_action_source_link,
                    "html_url": html_url,
                }

    return {"certainty": 0, "justification": []}


def deploy_command_subcheck(
    ctx: AnalyzeContext, ci_info: CIInfo, ci_service: BaseCIService, build_tool: BaseBuildTool
) -> dict:
    """Check for the use of deploy command to deploy."""
    check_certainty = 0.7
    for bash_cmd in ci_info["bash_commands"]:
        deploy_cmd = has_deploy_command(bash_cmd["commands"], build_tool)
        if deploy_cmd:
            # Get the permalink and HTML hyperlink tag of the CI file that triggered the bash command.
            trigger_link = ci_service.api_client.get_file_link(
                ctx.repo_full_name,
                ctx.commit_sha,
                ci_service.api_client.get_relative_path_of_workflow(os.path.basename(bash_cmd["CI_path"])),
            )
            # Get the permalink of the source file of the bash command.
            bash_source_link = ci_service.api_client.get_file_link(
                ctx.repo_full_name, ctx.commit_sha, bash_cmd["caller_path"]
            )

            html_url = ci_service.has_latest_run_passed(
                ctx.repo_full_name,
                ctx.branch_name,
                ctx.commit_sha,
                ctx.commit_date,
                os.path.basename(bash_cmd["CI_path"]),
            )

            justification: list[str | dict[str, str]] = [
                {
                    f"The target repository uses build tool {build_tool.name} to deploy": bash_source_link,
                    "The build is triggered by": trigger_link,
                },
                f"Deploy command: {deploy_cmd}",
                {"The status of the build can be seen at": html_url}
                if html_url
                else "However, could not find a passing workflow run.",
            ]
            return {
                "certainty": check_certainty,
                "justification": justification,
                "deploy_cmd": deploy_cmd,
                "trigger_link": trigger_link,
                "bash_source_link": bash_source_link,
                "html_url": html_url,
            }
    return {"certainty": 0, "justification": ""}


def deploy_kws_subcheck(ctx: AnalyzeContext, ci_service: BaseCIService, build_tool: BaseBuildTool) -> dict:
    """Check for the use of deploy keywords to deploy."""
    check_certainty = 0.6
    # We currently don't parse these CI configuration files.
    # We just look for a keyword for now.
    for unparsed_ci in (Jenkins, Travis, CircleCI, GitLabCI):
        if isinstance(ci_service, unparsed_ci):
            if build_tool.ci_deploy_kws[ci_service.name]:
                deploy_kw, config_name = ci_service.has_kws_in_config(
                    build_tool.ci_deploy_kws[ci_service.name], repo_path=ctx.repo_path
                )
                if not config_name:
                    return {"certainty": 0, "justification": ""}

                justification: list[str | dict[str, str]] = [f"The target repository uses {deploy_kw} to deploy."]

                return {
                    "certainty": check_certainty,
                    "justification": justification,
                    "deploy_kw": deploy_kw,
                    "config_name": config_name,
                }
    return {"certainty": 0, "justification": []}


class BuildAsCodeCheck(BaseCheck):
    """This class checks the build as code requirement.

    See https://slsa.dev/spec/v0.1/requirements#build-as-code.
    """

    def __init__(self) -> None:
        """Initiate the BuildAsCodeCheck instance."""
        description = (
            "The build definition and configuration executed by the build "
            "service is verifiably derived from text file definitions "
            "stored in a version control system."
        )
        depends_on = [
            ("mcn_trusted_builder_level_three_1", CheckResultType.FAILED),
        ]
        eval_reqs = [ReqName.BUILD_AS_CODE]
        self.confidence_score_threshold = 0.3

        super().__init__(
            check_id="mcn_build_as_code_1",
            description=description,
            depends_on=depends_on,
            eval_reqs=eval_reqs,
            result_on_skip=CheckResultType.PASSED,
        )

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
        # Get the build tool identified by the mcn_version_control_system_1, which we depend on.
        build_tool = ctx.dynamic_data["build_spec"].get("tool")
        ci_services = ctx.dynamic_data["ci_services"]

        # Checking if a build tool is discovered for this repo.
        if build_tool and not isinstance(build_tool, NoneBuildTool):
            for ci_info in ci_services:

                ci_service = ci_info["service"]
                # Checking if a CI service is discovered for this repo.
                if isinstance(ci_service, NoneCIService):
                    continue

                # Run subchecks
                ci_parsed = ci_parsed_subcheck(ci_info)
                deploy_action = deploy_action_subcheck(
                    ctx=ctx, ci_info=ci_info, ci_service=ci_service, build_tool=build_tool
                )
                deploy_command = deploy_command_subcheck(
                    ctx=ctx, ci_info=ci_info, ci_service=ci_service, build_tool=build_tool
                )
                deploy_kws = deploy_kws_subcheck(ctx=ctx, ci_service=ci_service, build_tool=build_tool)

                # Compile justifications from subchecks
                for subcheck in [ci_parsed, deploy_action, deploy_command, deploy_kws]:
                    check_result["justification"].extend(subcheck["justification"])

                deploy_source_link = deploy_cmd = html_url = trigger_link = ""

                # TODO: do we want to populate this information regardless of whether the check passes or not?
                if ctx.dynamic_data["is_inferred_prov"] and ci_info["provenances"]:

                    if ctx.dynamic_data["is_inferred_prov"] and ci_info["provenances"]:
                        predicate = ci_info["provenances"][0]["predicate"]
                        predicate["buildType"] = f"Custom {ci_service.name}"
                        predicate["invocation"]["configSource"][
                            "uri"
                        ] = f"{ctx.remote_path}@refs/heads/{ctx.branch_name}"
                        predicate["invocation"]["configSource"]["digest"]["sha1"] = ctx.commit_sha

                        # TODO: Change this. Need a better method for deciding which of the values to store.
                        # Could decide based on preliminary queries in the prolog string.
                        if deploy_action["certainty"]:
                            deploy_source_link = deploy_action["deploy_action_source_link"]
                            deploy_cmd = deploy_action["deploy_command"]
                            html_url = deploy_action["html_url"]
                            trigger_link = deploy_action["trigger_link"]
                            predicate["metadata"]["buildInvocationId"] = html_url
                            predicate["invocation"]["configSource"]["entryPoint"] = trigger_link
                            predicate["builder"]["id"] = deploy_source_link
                        elif deploy_command["certainty"]:
                            deploy_source_link = deploy_command["deploy_action_source_link"]
                            deploy_cmd = deploy_command["deploy_command"]
                            html_url = deploy_command["html_url"]
                            predicate["metadata"]["buildInvocationId"] = html_url
                            predicate["invocation"]["configSource"]["entryPoint"] = trigger_link
                            predicate["builder"]["id"] = deploy_source_link
                        elif deploy_kws["certainty"]:
                            deploy_cmd = deploy_kws["config_name"]
                            predicate["builder"]["id"] = deploy_command
                            predicate["invocation"]["configSource"]["entryPoint"] = deploy_command

                # TODO: BuildAsCodeTable should contain the results from subchecks and the confidence scores.
                # TODO: just decide on one deploy method to pass to the database.

                # Populate the BuildAsCodeSubchecks object with the certainty results from subchecks.
                bac_.build_as_code_subchecks = bac_.BuildAsCodeSubchecks(
                    ci_parsed=ci_parsed["certainty"],
                    deploy_action=deploy_action["certainty"],
                    deploy_command=deploy_command["certainty"],
                    deploy_kws=deploy_kws["certainty"],
                )

                prolog_string = PrologString(
                    """
                    :- use_module('src/macaron/slsa_analyzer/checks/problog_predicates.py').

                    A :: ci_parsed :- ci_parsed_check(A).
                    B :: deploy_action :- deploy_action_check(B).
                    C :: deploy_command :- deploy_command_check(C).
                    D :: deploy_kws :- deploy_kws_check(D).

                    0.80 :: deploy_action_certainty :- deploy_action.
                    0.15 :: deploy_action_certainty :- deploy_action, ci_parsed.

                    0.70 :: deploy_command_certainty :- deploy_command.
                    0.15 :: deploy_command_certainty :- deploy_command, ci_parsed.

                    0.60 :: deploy_kws_certainty :- deploy_kws.

                    build_as_code_check :- deploy_action_certainty; deploy_command_certainty; deploy_kws_certainty.

                    query(build_as_code_check).
                    """
                )

                # TODO: query each of the methods, and take the values from the one with the highest confidence.
                confidence_score = 0.0
                result = get_evaluatable().create_from(prolog_string).evaluate()
                for key, value in result.items():
                    if str(key) == "build_as_code_check":
                        confidence_score = float(value)
                    # logger.info("%s : %s", key, value)
                results = vars(bac_.build_as_code_subchecks)

                # TODO: Ideas:
                #  - Query the intermediate checks to construct the check_result table for the highest
                #       confidence score?
                #  - Can we find the evidence that contributes the most to this check to output the confidence
                #       scores for it, and populate the check_result table.
                #  - Print intermediate proofs?

                check_result["confidence_score"] = confidence_score

                subcheck_results: list[str | dict[str, str]] = [results]
                check_result["justification"].extend(subcheck_results)

                # TODO: Return subcheck certainties
                check_result["result_tables"] = [
                    BuildAsCodeTable(
                        build_tool_name=build_tool.name,
                        ci_service_name=ci_service.name,
                        build_trigger=trigger_link,
                        deploy_command=deploy_cmd,
                        build_status_url=html_url,
                        confidence_score=confidence_score,
                    )
                ]

                # Check whether the confidence score is greater than the minimum threshold for this check.
                if confidence_score >= self.confidence_score_threshold:
                    logger.info("The certainty of this check passing is: %s", confidence_score)
                    return CheckResultType.PASSED

            pass_msg = f"The target repository does not use {build_tool.name} to deploy."
            check_result["justification"].append(pass_msg)
            check_result["result_tables"] = [BuildAsCodeTable(build_tool_name=build_tool.name)]
            return CheckResultType.FAILED

        check_result["result_tables"] = [BuildAsCodeTable()]
        failed_msg = "The target repository does not have a build tool."
        check_result["justification"].append(failed_msg)
        return CheckResultType.FAILED


registry.register(BuildAsCodeCheck())
