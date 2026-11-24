"""Optional static Dozzle MCP token deployment contracts."""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import pytest
import yaml
from jinja2 import ChainableUndefined, Environment, StrictUndefined, Undefined

REPO = Path(__file__).resolve().parents[3]
ANSIBLE = REPO / "quickstart/ansible"
VM_ONE = REPO / "quickstart/vm_rawbbit_one"
VM_TWO = REPO / "quickstart/vm_rawbbit_two"


def render_env_template(path, **context):
    environment = Environment(
        undefined=ChainableUndefined,
        keep_trailing_newline=True,
    )

    def to_json(value, **options):
        if isinstance(value, Undefined):
            value = ""
        options.setdefault("ensure_ascii", False)
        return json.dumps(value, **options)

    environment.filters["to_json"] = to_json
    environment.filters["ternary"] = lambda value, yes, no: yes if value else no
    environment.filters["regex_replace"] = lambda value, pattern, replacement="": re.sub(
        pattern, replacement, str(value)
    )
    return environment.from_string(path.read_text()).render(**context)


def render_expression(expression, **context):
    environment = Environment(undefined=StrictUndefined)
    environment.filters["regex_search"] = lambda value, pattern: (
        match.group(0) if (match := re.search(pattern, str(value))) else None
    )
    return environment.from_string("{{ " + expression + " }}").render(**context)


def read_yaml(path):
    return yaml.safe_load(path.read_text())


def nested_tasks(tasks):
    for task in tasks:
        yield task
        for key in ("block", "rescue", "always"):
            yield from nested_tasks(task.get(key, []))


def token_value(rendered):
    line = next(
        line for line in rendered.splitlines() if line.startswith("DOZZLE_MCP_TOKEN=")
    )
    return json.loads(line.partition("=")[2])


def vm_token_validation(role, vm):
    tasks = read_yaml(ANSIBLE / f"roles/{role}/tasks/main.yml")
    name = f"Validate optional VM-{vm} Dozzle MCP token before deployment writes"
    task = next(task for task in tasks if task["name"] == name)
    assert task["no_log"] is True
    return task["ansible.builtin.assert"]["that"]


def marked_section(path, marker):
    text = path.read_text()
    start = f"<!-- {marker}:start -->"
    end = f"<!-- {marker}:end -->"
    assert text.count(start) == text.count(end) == 1
    return text.split(start, 1)[1].split(end, 1)[0]


def test_both_env_templates_render_empty_default_and_exact_token():
    cases = (
        (
            ANSIBLE / "roles/rawbbit_one/templates/env.j2",
            "vault_rawbbit_one_dozzle_mcp_token",
        ),
        (
            ANSIBLE / "roles/rawbbit_two/templates/env.j2",
            "vault_rawbbit_two_dozzle_mcp_token",
        ),
    )
    token = "synthetic-dozzle-mcp-token-0123456789abcdef"
    for template, variable in cases:
        assert token_value(render_env_template(template)) == ""
        assert token_value(render_env_template(template, **{variable: token})) == token


@unittest.skipUnless(shutil.which("docker"), "Docker Compose CLI not installed")
def test_compose_keeps_printable_token_characters_literal():
    token = "synthetic-dozzle-$RAWBBIT_TOKEN-é-0123456789abcdef"
    cases = (
        (VM_ONE, ANSIBLE / "roles/rawbbit_one/templates/env.j2",
         "vault_rawbbit_one_dozzle_mcp_token"),
        (VM_TWO, ANSIBLE / "roles/rawbbit_two/templates/env.j2",
         "vault_rawbbit_two_dozzle_mcp_token"),
    )
    for vm, template, variable in cases:
        rendered = render_env_template(
            template,
            **{
                variable: token,
                "rawbbit_one_dozzle_hostname": "logs.example.test",
                "rawbbit_two_dozzle_hostname": "logs.example.test",
            },
        )
        token_line = next(
            line for line in rendered.splitlines()
            if line.startswith("DOZZLE_MCP_TOKEN=")
        )
        with tempfile.TemporaryDirectory(prefix="dozzle-mcp-token-") as directory:
            # The base Compose file loads `.env` from its own folder, so run in a
            # scratch copy instead of depending on an operator's real `.env`.
            for compose_file in ("docker-compose.yml", "docker-compose.dozzle.yml"):
                shutil.copy(vm / compose_file, Path(directory) / compose_file)
            env_file = Path(directory) / ".env"
            env_example = (vm / ".env.example").read_text()
            env_file.write_text(
                env_example.replace("DOZZLE_MCP_TOKEN=\n", token_line + "\n")
            )
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "--env-file",
                    str(env_file),
                    "-f",
                    "docker-compose.yml",
                    "-f",
                    "docker-compose.dozzle.yml",
                    "config",
                    "--format",
                    "json",
                ],
                cwd=directory,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            environment = json.loads(result.stdout)["services"]["dozzle"]["environment"]
            assert environment["DOZZLE_MCP_TOKEN"] == token.replace("$", "$$")


def test_both_dozzle_overlays_use_optional_token_interpolation():
    expected = "${DOZZLE_MCP_TOKEN:-}"
    for vm in (VM_ONE, VM_TWO):
        compose = read_yaml(vm / "docker-compose.dozzle.yml")
        assert (
            compose["services"]["dozzle"]["environment"]["DOZZLE_MCP_TOKEN"]
            == expected
        )


def test_vm_two_staged_and_activation_compose_use_the_rendered_env_file():
    expected = "{{ rawbbit_two_stage.path }}/.env"
    for path in (
        ANSIBLE / "roles/rawbbit_two/tasks/main.yml",
        ANSIBLE / "roles/rawbbit_two/tasks/activate-datasets.yml",
        ANSIBLE / "roles/rawbbit_two/tasks/activate-empty-datasets.yml",
    ):
        tasks = nested_tasks(read_yaml(path))
        compose_tasks = [
            task
            for task in tasks
            if "community.docker.docker_compose_v2" in task
        ]
        assert compose_tasks
        for task in compose_tasks:
            assert task["community.docker.docker_compose_v2"]["env_files"] == [
                expected
            ]

    main_tasks = read_yaml(ANSIBLE / "roles/rawbbit_two/tasks/main.yml")
    validation = next(
        task
        for task in main_tasks
        if task["name"] == "Validate base VM-two Compose configuration before starting ClickHouse"
    )
    argv = validation["ansible.builtin.command"]["argv"]
    env_file_index = argv.index("--env-file")
    assert argv[env_file_index + 1] == expected

    helper = (VM_TWO / "deploy-dbt-runner.py").read_text()
    assert '"--env-file", str(directory / ".env")' in helper


def test_vm_token_validations_are_no_log_and_reject_unsafe_values():
    for role, vm, variable in (
        (
            "rawbbit_one",
            "one",
            "vault_rawbbit_one_dozzle_mcp_token",
        ),
        (
            "rawbbit_two",
            "two",
            "vault_rawbbit_two_dozzle_mcp_token",
        ),
    ):
        conditions = vm_token_validation(role, vm)
        expression = " and ".join(conditions)
        assert render_expression(expression) == "True"
        assert render_expression(expression, **{variable: ""}) == "True"
        for invalid in (
            "x" * 31,
            "x" * 31 + " ",
            "change_me" + "x" * 32,
        ):
            assert render_expression(expression, **{variable: invalid}) == "False"


def test_vault_example_has_both_optional_empty_tokens():
    vault = read_yaml(ANSIBLE / "group_vars/all/vault.yml.example")
    assert vault["vault_rawbbit_one_dozzle_mcp_token"] == ""
    assert vault["vault_rawbbit_two_dozzle_mcp_token"] == ""
    text = (ANSIBLE / "group_vars/all/vault.yml.example").read_text()
    assert "Optional static Dozzle MCP token; generate with `openssl rand -hex 32`." in text
    assert "Leave empty to disable." in text


@pytest.mark.skipif(
    not (REPO / "docs/oss").is_dir(),
    reason="docs/oss mirrors exist only in the internal repo",
)
def test_ansible_readme_mirror_and_vm_two_marked_sections_stay_in_parity():
    internal_ansible = ANSIBLE / "README.md"
    oss_ansible = REPO / "docs/oss/quickstart/ansible/README.md"
    assert internal_ansible.read_bytes() == oss_ansible.read_bytes()

    internal_vm_two = VM_TWO / "README.md"
    oss_vm_two = REPO / "docs/oss/quickstart/vm_rawbbit_two/README.md"
    for marker in ("dataset-mcp", "dbt-routing"):
        assert marked_section(internal_vm_two, marker) == marked_section(
            oss_vm_two, marker
        )


def test_manual_env_examples_leave_static_token_disabled():
    for vm in (VM_ONE, VM_TWO):
        assert "DOZZLE_MCP_TOKEN=\n" in (vm / ".env.example").read_text()
