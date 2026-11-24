"""Beszel quickstart role contracts and real Jinja template rendering."""

import json
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

REPO = Path(__file__).resolve().parents[3]
HAS_OSS_MIRRORS = (REPO / "docs/oss").is_dir()
ANSIBLE = REPO / "quickstart/ansible"
AGENT_ROLE = ANSIBLE / "roles/beszel_agent"


def read_yaml(path):
    return yaml.safe_load(path.read_text())


def render_template(path, **context):
    environment = Environment(undefined=StrictUndefined, keep_trailing_newline=True)
    environment.filters["to_json"] = lambda value, **options: json.dumps(value, **options)
    return environment.from_string(path.read_text()).render(**context)


def render_expression(expression, **context):
    environment = Environment(undefined=StrictUndefined)

    def regex_search(value, pattern):
        match = re.search(pattern, str(value))
        return match.group(0) if match else None

    environment.filters["regex_search"] = regex_search
    return environment.from_string("{{ " + expression + " }}").render(**context)


def role_tasks():
    return read_yaml(AGENT_ROLE / "tasks/main.yml")


class BeszelAgentContracts(unittest.TestCase):
    def test_global_defaults_vm_overrides_and_examples(self):
        shared = read_yaml(AGENT_ROLE / "defaults/main.yml")
        one = read_yaml(ANSIBLE / "roles/rawbbit_one/defaults/main.yml")
        two = read_yaml(ANSIBLE / "roles/rawbbit_two/defaults/main.yml")
        example = read_yaml(ANSIBLE / "group_vars/all/main.yml.example")
        vault_example = read_yaml(ANSIBLE / "group_vars/all/vault.yml.example")

        self.assertEqual(shared["rawbbit_beszel_hub_url"], "")
        self.assertEqual(shared["rawbbit_beszel_hub_key"], "")
        for defaults, prefix, system in (
            (one, "rawbbit_one", "rawbbit-one"),
            (two, "rawbbit_two", "rawbbit-two"),
        ):
            self.assertEqual(
                defaults[f"{prefix}_beszel_enabled"],
                "{{ rawbbit_beszel_enabled | default(false) }}",
            )
            self.assertEqual(defaults[f"{prefix}_beszel_system_name"], system)
            self.assertEqual(defaults[f"{prefix}_beszel_smart_devices"], [])
            self.assertEqual(example[f"{prefix}_beszel_system_name"], system)
            self.assertEqual(example[f"{prefix}_beszel_smart_devices"], [])

        self.assertIs(example["rawbbit_beszel_enabled"], False)
        self.assertEqual(example["rawbbit_beszel_hub_url"], "")
        self.assertEqual(example["rawbbit_beszel_hub_key"], "")
        self.assertEqual(
            vault_example["vault_rawbbit_beszel_token"],
            "change_me_beszel_universal_token",
        )
        self.assertEqual(
            Environment().from_string(one["rawbbit_one_beszel_enabled"]).render(),
            "False",
        )
        self.assertEqual(
            Environment()
            .from_string(two["rawbbit_two_beszel_enabled"])
            .render(rawbbit_beszel_enabled=True),
            "True",
        )

    def test_role_includes_follow_stack_start_and_flush_anchors(self):
        for vm, prefix, anchor in (
            (
                "one",
                "rawbbit_one",
                "Apply pending VM-one service handlers",
            ),
            (
                "two",
                "rawbbit_two",
                "Apply pending VM-two service handlers",
            ),
        ):
            tasks = read_yaml(ANSIBLE / f"roles/{prefix}/tasks/main.yml")
            anchor_index = next(i for i, task in enumerate(tasks) if task["name"] == anchor)
            include_index = next(
                i
                for i, task in enumerate(tasks)
                if task.get("ansible.builtin.include_role", {}).get("name")
                == "beszel_agent"
            )
            self.assertLess(anchor_index, include_index)
            include = tasks[include_index]
            self.assertEqual(
                set(include["vars"]),
                {
                    "beszel_agent_enabled",
                    "beszel_agent_system_name",
                    "beszel_agent_smart_devices",
                },
            )
            self.assertIn(f"{prefix}_beszel_enabled", include["vars"]["beszel_agent_enabled"])
            self.assertIn(f"{prefix}_beszel_system_name", include["vars"]["beszel_agent_system_name"])
            self.assertIn(f"{prefix}_beszel_smart_devices", include["vars"]["beszel_agent_smart_devices"])
            self.assertNotIn("vault_rawbbit_beszel_token", str(include))

        two_tasks = read_yaml(ANSIBLE / "roles/rawbbit_two/tasks/main.yml")
        dozzle_index = next(
            i for i, task in enumerate(two_tasks)
            if task["name"] == "Verify authenticated Dozzle endpoint is reachable"
        )
        flush_index = next(
            i for i, task in enumerate(two_tasks)
            if task["name"] == "Apply pending VM-two service handlers"
        )
        self.assertLess(dozzle_index, flush_index)

    def test_enabled_and_disabled_paths_are_isolated(self):
        tasks = role_tasks()
        enabled = tasks[0]
        self.assertEqual(enabled["when"], "beszel_agent_enabled | bool")
        block = enabled["block"]
        validation_index = next(
            i for i, task in enumerate(block)
            if task["name"] == "Validate Beszel hub, registration secrets, and system name"
        )
        directory_index = next(
            i for i, task in enumerate(block)
            if task["name"] == "Create persistent Beszel agent directories"
        )
        env_index = next(
            i for i, task in enumerate(block)
            if task["name"] == "Render protected Beszel agent credentials"
        )
        compose_index = next(
            i for i, task in enumerate(block)
            if task["name"] == "Render isolated Beszel agent Compose project"
        )
        reconcile_index = next(
            i for i, task in enumerate(block)
            if task["name"] == "Reconcile only the Beszel agent Compose project"
        )
        self.assertLess(validation_index, directory_index)
        self.assertLess(directory_index, env_index)
        self.assertLess(env_index, compose_index)
        self.assertLess(compose_index, reconcile_index)

        disabled_stat, disabled_stop = tasks[1:]
        self.assertEqual(
            disabled_stat["ansible.builtin.stat"]["path"],
            "{{ beszel_agent_root }}/docker-compose.yml",
        )
        self.assertEqual(disabled_stat["when"], "not (beszel_agent_enabled | bool)")
        stop = disabled_stop["community.docker.docker_compose_v2"]
        self.assertEqual(stop["state"], "absent")
        self.assertIs(stop["remove_volumes"], False)
        self.assertEqual(
            disabled_stop["when"],
            [
                "not (beszel_agent_enabled | bool)",
                "beszel_agent_compose_file.stat.exists",
            ],
        )
        self.assertTrue(disabled_stop["no_log"])
        self.assertNotIn("vault_rawbbit_beszel_token", str(tasks[1:]))
        self.assertNotIn("rawbbit_beszel_hub_key", str(tasks[1:]))

    def test_secrets_are_validated_and_protected_before_writes(self):
        block = role_tasks()[0]["block"]
        validate = block[0]
        conditions = "\n".join(validate["ansible.builtin.assert"]["that"])
        self.assertTrue(validate["no_log"])
        for expected in (
            "vault_rawbbit_beszel_token | default('')",
            "rawbbit_beszel_hub_url",
            "rawbbit_beszel_hub_key",
            "beszel_agent_system_name",
            "'\n'",
            "'\r'",
            "https://",
            "regex_search",
            r"[\x00-\x1f\x7f-\x9f]",
        ):
            self.assertIn(expected, conditions)
        control_check = next(
            condition for condition in validate["ansible.builtin.assert"]["that"]
            if "vault_rawbbit_beszel_token" in condition
            and "regex_search" in condition
            and r"\x00" in condition
        )
        for token, valid in (
            ("synthetic-token-é", True),
            ("synthetic-token\x01", False),
            ("synthetic-token\x9f", False),
        ):
            self.assertEqual(
                render_expression(
                    control_check,
                    vault_rawbbit_beszel_token=token,
                ),
                str(valid),
            )

        env_task = next(
            task for task in block
            if task["name"] == "Render protected Beszel agent credentials"
        )
        self.assertTrue(env_task["no_log"])
        env = env_task["ansible.builtin.template"]
        self.assertEqual(env["dest"], "{{ beszel_agent_root }}/agent.env")
        self.assertEqual(env["mode"], "0600")
        compose_task = next(
            task for task in block
            if task["name"] == "Reconcile only the Beszel agent Compose project"
        )
        self.assertTrue(compose_task["no_log"])

    def test_device_validation_is_strict_and_requires_host_devices(self):
        block = role_tasks()[0]["block"]
        device_task = next(
            task for task in block
            if task["name"] == "Validate each configured SMART device mapping"
        )
        condition = "\n".join(device_task["ansible.builtin.assert"]["that"])
        match = re.search(r"is match\('([^']+)'\)", condition)
        self.assertIsNotNone(match)
        device_pattern = re.compile(match.group(1))
        for valid in (
            "/dev/sda",
            "/dev/nvme0n1:/dev/nvme0n1",
            "/dev/disk/by-id/nvme-abc:/dev/nvme0",
        ):
            self.assertRegex(valid, device_pattern)
        for invalid in (
            "/dev/sda:rwm",
            "/dev/../sda",
            "/dev/sda:/dev/../other",
            "/dev/sda:/dev/sdb:ro",
            "/dev/sda with-space",
            "sda",
        ):
            self.assertNotRegex(invalid, device_pattern)

        source_stat = next(
            task for task in block
            if task["name"] == "Inspect configured SMART device sources on the host"
        )
        self.assertEqual(
            source_stat["ansible.builtin.stat"]["path"],
            "{{ item.split(':')[0] }}",
        )
        self.assertIs(source_stat["ansible.builtin.stat"]["follow"], True)
        host_check = next(
            task for task in block
            if task["name"] == "Require each SMART source to be an existing character or block device"
        )
        checks = "\n".join(host_check["ansible.builtin.assert"]["that"])
        self.assertIn("item.stat.exists", checks)
        self.assertIn("item.stat.isblk", checks)
        self.assertIn("item.stat.ischr", checks)
        with tempfile.TemporaryDirectory() as directory:
            device_link = Path(directory) / "host-device"
            device_link.symlink_to("/dev/null")
            self.assertTrue(stat.S_ISCHR(device_link.stat().st_mode))

    def test_rendered_agent_compose_has_smart_off_and_on_contracts(self):
        base = {
            "beszel_agent_project_name": "beszel-agent",
            "beszel_agent_image": "henrygd/beszel-agent:0.21.2-alpine",
            "beszel_agent_root": "/srv/beszel-agent",
            "rawbbit_beszel_hub_url": "https://beszel.example.test",
            "beszel_agent_system_name": "rawbbit-one",
            "beszel_agent_smart_devices": [],
        }
        template = AGENT_ROLE / "templates/docker-compose.yml.j2"
        off_yaml = render_template(template, **base)
        off = yaml.safe_load(off_yaml)
        service = off["services"]["beszel-agent"]
        self.assertEqual(off["name"], "beszel-agent")
        self.assertEqual(service["image"], base["beszel_agent_image"])
        self.assertEqual(service["network_mode"], "host")
        self.assertEqual(service["restart"], "unless-stopped")
        self.assertEqual(service["env_file"], ["/srv/beszel-agent/agent.env"])
        self.assertEqual(service["environment"]["DISABLE_SSH"], "true")
        self.assertEqual(service["environment"]["SMART_DEVICES"], "")
        self.assertNotIn("devices", service)
        self.assertNotIn("cap_add", service)
        self.assertNotIn("ports", service)
        self.assertNotIn("LISTEN", off_yaml)

        volumes = service["volumes"]
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock:ro", volumes)
        self.assertIn("/srv/beszel-agent/data:/var/lib/beszel-agent", volumes)
        self.assertEqual(service["logging"]["driver"], "json-file")
        self.assertEqual(
            service["logging"]["options"],
            {"max-size": "10m", "max-file": "5"},
        )

        smart_context = dict(
            base,
            beszel_agent_smart_devices=[
                "/dev/nvme0n1:/dev/nvme0",
                "/dev/sda",
            ],
        )
        smart = yaml.safe_load(render_template(template, **smart_context))
        smart_service = smart["services"]["beszel-agent"]
        self.assertEqual(
            smart_service["environment"]["SMART_DEVICES"],
            "/dev/nvme0,/dev/sda",
        )
        self.assertEqual(
            smart_service["devices"],
            ["/dev/nvme0n1:/dev/nvme0", "/dev/sda:/dev/sda"],
        )
        self.assertEqual(smart_service["cap_add"], ["SYS_RAWIO", "SYS_ADMIN"])

    def test_credentials_are_only_in_protected_env_and_rotation_isolated(self):
        env_template = AGENT_ROLE / "templates/agent.env.j2"
        compose_template = AGENT_ROLE / "templates/docker-compose.yml.j2"
        env_a = render_template(
            env_template,
            vault_rawbbit_beszel_token="synthetic-token-a",
            rawbbit_beszel_hub_key="synthetic-key-a",
        )
        env_b = render_template(
            env_template,
            vault_rawbbit_beszel_token="synthetic-token-b",
            rawbbit_beszel_hub_key="synthetic-key-b",
        )
        self.assertNotEqual(env_a, env_b)
        self.assertIn('TOKEN="synthetic-token-a"', env_a)
        self.assertIn('KEY="synthetic-key-a"', env_a)

        context = {
            "beszel_agent_project_name": "beszel-agent",
            "beszel_agent_image": "henrygd/beszel-agent:0.21.2-alpine",
            "beszel_agent_root": "/srv/beszel-agent",
            "rawbbit_beszel_hub_url": "https://beszel.example.test",
            "beszel_agent_system_name": "rawbbit-one",
            "beszel_agent_smart_devices": [],
        }
        compose = render_template(compose_template, **context)
        self.assertNotIn("synthetic-token", compose)
        self.assertNotIn("synthetic-key", compose)
        self.assertNotRegex(compose_template.read_text(), r"(?m)^\s*(TOKEN|KEY)\s*[:=]")
        self.assertIn("/srv/beszel-agent/agent.env", compose)

        reconcile = next(
            task for task in role_tasks()[0]["block"]
            if task["name"] == "Reconcile only the Beszel agent Compose project"
        )
        module = reconcile["community.docker.docker_compose_v2"]
        self.assertEqual(module["project_name"], "{{ beszel_agent_project_name }}")
        self.assertEqual(module["pull"], "missing")
        self.assertEqual(module["state"], "present")
        self.assertIs(module["wait"], True)
        self.assertEqual(module["wait_timeout"], 60)
        self.assertNotIn("recreate", module)
        self.assertTrue(reconcile["no_log"])

    @unittest.skipUnless(shutil.which("docker"), "Docker Compose CLI not installed")
    def test_compose_parser_preserves_synthetic_dollar_values(self):
        token = "synthetic-é-$TOKEN_NAME\\'quoted"
        key = "synthetic-ø-$KEY_NAME\\"
        hub_url = "https://beszel.example.test/path/$HUB_NAME"
        system_name = "rawbbit-$SYSTEM_NAME"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agent.env").write_text(
                render_template(
                    AGENT_ROLE / "templates/agent.env.j2",
                    vault_rawbbit_beszel_token=token,
                    rawbbit_beszel_hub_key=key,
                )
            )
            compose = render_template(
                AGENT_ROLE / "templates/docker-compose.yml.j2",
                beszel_agent_project_name="beszel-agent",
                beszel_agent_image="henrygd/beszel-agent:0.21.2-alpine",
                beszel_agent_root=str(root),
                rawbbit_beszel_hub_url=hub_url,
                beszel_agent_system_name=system_name,
                beszel_agent_smart_devices=[],
            )
            compose_path = root / "docker-compose.yml"
            compose_path.write_text(compose)
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(compose_path),
                    "config",
                    "--format",
                    "json",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            service = json.loads(result.stdout)["services"]["beszel-agent"]
            parsed = service["environment"]
            self.assertEqual(parsed["TOKEN"], token.replace("$", "$$"))
            self.assertEqual(parsed["KEY"], key.replace("$", "$$"))
            self.assertEqual(parsed["HUB_URL"], hub_url.replace("$", "$$"))
            self.assertEqual(parsed["SYSTEM_NAME"], system_name.replace("$", "$$"))

    def test_hub_compose_caddy_and_documentation_mirrors(self):
        hub = REPO / "quickstart/beszel_hub"
        compose = read_yaml(hub / "docker-compose.yml")
        services = compose["services"]
        self.assertEqual(services["beszel"]["image"], "henrygd/beszel:0.21.2")
        self.assertNotIn("ports", services["beszel"])
        self.assertEqual(
            services["beszel"]["environment"]["APP_URL"],
            "${BESZEL_APP_URL:?Set BESZEL_APP_URL in .env}",
        )
        self.assertEqual(services["beszel"]["environment"]["DISABLE_SSH"], "true")
        self.assertIn("./beszel_data:/beszel_data", services["beszel"]["volumes"])
        self.assertEqual(services["caddy"]["image"], "caddy:2-alpine")
        self.assertEqual(services["caddy"]["ports"], ["80:80", "443:443"])
        self.assertEqual(
            services["caddy"]["environment"]["BESZEL_DOMAIN"],
            "${BESZEL_DOMAIN:?Set BESZEL_DOMAIN in .env}",
        )
        self.assertIn("./caddy/data:/data", services["caddy"]["volumes"])
        self.assertIn("./caddy/config:/config", services["caddy"]["volumes"])
        self.assertEqual(services["beszel"]["networks"], ["beszel-private"])
        self.assertEqual(services["caddy"]["networks"], ["beszel-private"])

        caddyfile = (hub / "Caddyfile").read_text()
        self.assertIn("{$BESZEL_DOMAIN}", caddyfile)
        self.assertIn("reverse_proxy beszel:8090", caddyfile)
        self.assertIn("read_timeout 360s", caddyfile)
        self.assertIn("WebSocket", caddyfile)

        # The docs/oss mirrors exist only in the internal repo; the public repo
        # is promoted from them, so it has no second copy to compare.
        if HAS_OSS_MIRRORS:
            for relative in (
                "quickstart/ansible/README.md",
                "quickstart/beszel_hub/docker-compose.yml",
                "quickstart/beszel_hub/Caddyfile",
                "quickstart/beszel_hub/.env.example",
                "quickstart/beszel_hub/README.md",
            ):
                source = REPO / relative
                mirror = REPO / "docs/oss" / relative
                self.assertEqual(source.read_bytes(), mirror.read_bytes(), relative)

    def test_vm_document_links(self):
        for vm in ("vm_rawbbit_one", "vm_rawbbit_two"):
            paths = [REPO / f"quickstart/{vm}/README.md"]
            if HAS_OSS_MIRRORS:
                paths.append(REPO / f"docs/oss/quickstart/{vm}/README.md")
            for path in paths:
                content = path.read_text()
                self.assertIn("## Optional Beszel Host Monitoring", content)
                self.assertIn("../beszel_hub/README.md", content)
                self.assertIn("../ansible/README.md#optional-beszel-monitoring", content)

    @unittest.skipUnless(HAS_OSS_MIRRORS, "docs/oss mirrors exist only in the internal repo")
    def test_vm_two_marker_parity(self):
        vm_two = REPO / "quickstart/vm_rawbbit_two/README.md"
        vm_two_oss = REPO / "docs/oss/quickstart/vm_rawbbit_two/README.md"
        marker_pattern = re.compile(r"<!-- ([^ ]+):(start|end) -->")
        internal_markers = marker_pattern.findall(vm_two.read_text())
        oss_markers = marker_pattern.findall(vm_two_oss.read_text())
        self.assertEqual(internal_markers, oss_markers)
        self.assertEqual(
            sorted({name for name, _ in internal_markers}),
            ["dataset-mcp", "dbt-routing"],
        )
        self.assertEqual(
            read_yaml(ANSIBLE / "roles/rawbbit_one/defaults/main.yml")[
                "rawbbit_one_beszel_system_name"
            ],
            "rawbbit-one",
        )


if __name__ == "__main__":
    unittest.main()
