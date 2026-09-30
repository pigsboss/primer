# -*- coding: utf-8 -*-
"""配置模块：层叠顺序、深合并、类型校验、``key_env`` 派生、``temperature``、缺密钥点名
变量、``--show`` 不打印密钥且全英文。

全部注入：``environ`` 传假映射、配置文件写在 ``tmp_path`` 里，既不读真的
``~/.config``，也不在测试进程里设环境变量。唯一例外是最后那个子进程用例，它跑的是
用户真正会敲的命令 ``python3 -m primer.config --show``，并把 ``XDG_CONFIG_HOME``
在子进程环境里指到 ``tmp_path``——进程自己的环境一个字都不动。

消息一律英文是 ``docs/guides/CODING_STANDARDS.md`` §2.2 的要求，两个 ``*_ascii_only``
用例把它变成机械检查：报告行与各类报错里出现一个非 ASCII 字符就红。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

from primer.config import (
    BUILTIN_PROVIDERS,
    Config,
    ConfigError,
    Endpoint,
    api_key,
    load_config,
    main,
    resolve_role,
    show_report,
)

MACHINE_REL = ("primer", "config.yaml")
PROJECT_REL = ("_primer", "config.yaml")

MACHINE_YAML = """\
providers:
  deepseek:
    base_url: https://machine.example/v1
    key_env: MACHINE_KEY
roles:
  select: {provider: deepseek, model: machine-model}
"""

PROJECT_YAML = """\
roles:
  select: {model: project-model}
"""

EXPLICIT_YAML = """\
roles:
  select: {model: explicit-model}
"""


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def fake_environ(tmp_path: Path, *, xdg: str = "xdg", home: Optional[Path] = None) -> dict:
    """假环境：``XDG_CONFIG_HOME`` 指到 ``tmp_path`` 下，机器级配置不可能落在真家里。"""
    env = {"XDG_CONFIG_HOME": str(tmp_path / xdg)}
    if home is not None:
        env["HOME"] = str(home)
    return env


def project_root(tmp_path: Path) -> Path:
    return tmp_path / "proj"


def machine_config(tmp_path: Path, text: str) -> Path:
    return write(tmp_path / "xdg" / Path(*MACHINE_REL), text)


def project_config(tmp_path: Path, text: str) -> Path:
    return write(project_root(tmp_path) / Path(*PROJECT_REL), text)


# ---------------------------------------------------------------- 层叠


def test_builtin_defaults_are_the_only_thing_when_no_file_exists(tmp_path):
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    assert set(config.providers) == set(BUILTIN_PROVIDERS) == {"deepseek"}
    assert config.providers["deepseek"].base_url == "https://api.deepseek.com"
    assert config.providers["deepseek"].key_env == "PRIMER_DEEPSEEK_API_KEY"
    assert config.roles == {}
    assert config.files == ()
    assert config.project_root == project_root(tmp_path)


def test_machine_layer_overrides_the_builtin(tmp_path):
    machine = machine_config(tmp_path, MACHINE_YAML)
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    assert config.files == (machine,)
    endpoint = resolve_role(config, "select")
    assert endpoint.provider == "deepseek"
    assert endpoint.base_url == "https://machine.example/v1"
    assert endpoint.key_env == "MACHINE_KEY"
    assert endpoint.model == "machine-model"


def test_project_layer_overrides_the_machine_layer(tmp_path):
    machine = machine_config(tmp_path, MACHINE_YAML)
    project = project_config(tmp_path, PROJECT_YAML)
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    assert config.files == (machine, project)
    endpoint = resolve_role(config, "select")
    assert endpoint.model == "project-model"
    # 项目级只改了 model，端点与密钥变量名仍是机器级给的那份。
    assert endpoint.base_url == "https://machine.example/v1"
    assert endpoint.key_env == "MACHINE_KEY"


def test_explicit_file_overrides_the_project_layer(tmp_path):
    machine = machine_config(tmp_path, MACHINE_YAML)
    project = project_config(tmp_path, PROJECT_YAML)
    explicit = write(tmp_path / "extra.yaml", EXPLICIT_YAML)
    config = load_config(project_root(tmp_path), explicit, environ=fake_environ(tmp_path))

    assert config.files == (machine, project, explicit)
    assert resolve_role(config, "select").model == "explicit-model"


def test_command_line_overrides_beat_every_file(tmp_path):
    machine_config(tmp_path, MACHINE_YAML)
    project_config(tmp_path, PROJECT_YAML)
    explicit = write(tmp_path / "extra.yaml", EXPLICIT_YAML)
    config = load_config(project_root(tmp_path), explicit, environ=fake_environ(tmp_path))

    endpoint = resolve_role(
        config,
        "select",
        provider="deepseek",
        model="cli-model",
        base_url="https://cli.example/v1",
        key_env="CLI_KEY",
        max_tokens=4096,
    )

    assert endpoint == Endpoint(
        role="select",
        provider="deepseek",
        base_url="https://cli.example/v1",
        model="cli-model",
        key_env="CLI_KEY",
        max_tokens=4096,
        batch_size=None,
        temperature=None,
    )


def test_overrides_supply_a_role_that_no_file_defines(tmp_path):
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    endpoint = resolve_role(config, "select", provider="deepseek", model="cli-model")

    assert endpoint.base_url == "https://api.deepseek.com"
    assert endpoint.key_env == "PRIMER_DEEPSEEK_API_KEY"
    assert endpoint.model == "cli-model"


# ---------------------------------------------------------------- 深合并


def test_deep_merge_keeps_the_sibling_keys_of_a_section(tmp_path):
    machine_config(
        tmp_path,
        """\
providers:
  moonshot:
    base_url: https://api.moonshot.cn/v1
    key_env: PRIMER_MOONSHOT_API_KEY
roles:
  distill: {provider: moonshot, model: kimi-k2-0905-preview}
""",
    )
    project_config(
        tmp_path,
        """\
providers:
  deepseek:
    key_env: PROJECT_DEEPSEEK_KEY
roles:
  distill: {max_tokens: 32768, batch_size: 4}
""",
    )
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    # provider 表没被整块替换：机器级的 moonshot 还在，deepseek 只换了 key_env。
    assert set(config.providers) == {"deepseek", "moonshot"}
    assert config.providers["deepseek"].key_env == "PROJECT_DEEPSEEK_KEY"
    assert config.providers["deepseek"].base_url == "https://api.deepseek.com"
    assert config.providers["moonshot"].base_url == "https://api.moonshot.cn/v1"
    assert config.providers["moonshot"].key_env == "PRIMER_MOONSHOT_API_KEY"

    # role 表同理：项目级只补了两个旋钮，provider 与 model 还是机器级那对。
    endpoint = resolve_role(config, "distill")
    assert endpoint.provider == "moonshot"
    assert endpoint.model == "kimi-k2-0905-preview"
    assert endpoint.base_url == "https://api.moonshot.cn/v1"
    assert endpoint.key_env == "PRIMER_MOONSHOT_API_KEY"
    assert endpoint.max_tokens == 32768
    assert endpoint.batch_size == 4


def test_max_tokens_override_beats_the_role_definition(tmp_path):
    project_config(
        tmp_path, "roles:\n  distill: {provider: deepseek, model: m, max_tokens: 32768}\n"
    )
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    assert resolve_role(config, "distill").max_tokens == 32768
    assert resolve_role(config, "distill", max_tokens=512).max_tokens == 512


def test_unknown_keys_are_ignored(tmp_path):
    project_config(
        tmp_path,
        """\
schema: 2
providers:
  deepseek:
    base_url: https://api.deepseek.com
    region: cn
roles:
  select: {provider: deepseek, model: m, retries: 3}
""",
    )
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    assert resolve_role(config, "select").model == "m"


def test_a_later_layer_replaces_a_scalar_with_a_mapping_wholesale(tmp_path):
    machine_config(
        tmp_path, "providers:\n  deepseek:\n    base_url: https://machine.example/v1\n"
    )
    project_config(
        tmp_path, "providers:\n  deepseek:\n    base_url: https://project.example/v1\n"
    )
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    assert config.providers["deepseek"].base_url == "https://project.example/v1"


# ---------------------------------------------------------------- key_env 派生


def test_key_env_is_derived_from_the_provider_name(tmp_path):
    project_config(
        tmp_path,
        """\
providers:
  moonshot:
    base_url: https://api.moonshot.cn/v1
  kimi:
    base_url: https://api.kimi.com/coding/v1
roles:
  distill: {provider: moonshot, model: kimi-k2-0905-preview}
  coding: {provider: kimi, model: k3}
""",
    )
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    assert config.providers["moonshot"].key_env == "PRIMER_MOONSHOT_API_KEY"
    assert config.providers["kimi"].key_env == "PRIMER_KIMI_API_KEY"
    assert resolve_role(config, "distill").key_env == "PRIMER_MOONSHOT_API_KEY"
    assert resolve_role(config, "coding").key_env == "PRIMER_KIMI_API_KEY"


def test_the_derived_name_folds_non_alphanumeric_characters(tmp_path):
    project_config(
        tmp_path,
        """\
providers:
  api.moonshot-cn:
    base_url: https://api.moonshot.cn/v1
roles:
  distill: {provider: api.moonshot-cn, model: m}
""",
    )
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    assert config.providers["api.moonshot-cn"].key_env == "PRIMER_API_MOONSHOT_CN_API_KEY"
    assert resolve_role(config, "distill").key_env == "PRIMER_API_MOONSHOT_CN_API_KEY"


def test_an_explicit_key_env_wins_over_the_derived_name(tmp_path):
    project_config(
        tmp_path,
        """\
providers:
  kimi:
    base_url: https://api.kimi.com/coding/v1
    key_env: PRIMER_MOONSHOT_API_KEY
roles:
  distill: {provider: kimi, model: k3}
""",
    )
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    assert config.providers["kimi"].key_env == "PRIMER_MOONSHOT_API_KEY"
    assert resolve_role(config, "distill").key_env == "PRIMER_MOONSHOT_API_KEY"


def test_the_builtin_provider_derives_its_key_variable(tmp_path):
    """没有任何配置文件时也拿得到变量名——内置 deepseek 的那把是派生出来的。"""
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))
    endpoint = resolve_role(config, "select", provider="deepseek", model="m")

    with pytest.raises(ConfigError) as excinfo:
        api_key(endpoint, environ={})

    assert "PRIMER_DEEPSEEK_API_KEY" in str(excinfo.value)


def test_a_provider_name_that_derives_nothing_is_rejected(tmp_path):
    path = project_config(tmp_path, 'providers:\n  "---":\n    base_url: https://x/v1\n')

    with pytest.raises(ConfigError) as excinfo:
        load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    message = str(excinfo.value)
    assert str(path) in message
    assert "no letters or digits" in message


# ---------------------------------------------------------------- temperature


def test_provider_temperature_reaches_the_endpoint(tmp_path):
    project_config(
        tmp_path,
        """\
providers:
  kimi:
    base_url: https://api.kimi.com/coding/v1
    temperature: 1
roles:
  distill: {provider: kimi, model: k3}
""",
    )
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    assert config.providers["kimi"].temperature == 1
    assert resolve_role(config, "distill").temperature == 1


def test_role_temperature_overrides_the_provider(tmp_path):
    project_config(
        tmp_path,
        """\
providers:
  kimi:
    base_url: https://api.kimi.com/coding/v1
    temperature: 1
roles:
  distill: {provider: kimi, model: k3, temperature: 0}
""",
    )
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    assert resolve_role(config, "distill").temperature == 0


def test_temperature_is_none_when_nobody_sets_it(tmp_path):
    project_config(tmp_path, "roles:\n  select: {provider: deepseek, model: m}\n")
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    assert config.providers["deepseek"].temperature is None
    assert resolve_role(config, "select").temperature is None


def test_a_float_temperature_is_accepted(tmp_path):
    project_config(
        tmp_path,
        """\
providers:
  x:
    base_url: https://x/v1
    temperature: 0.7
roles:
  r: {provider: x, model: m}
""",
    )
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    assert resolve_role(config, "r").temperature == 0.7


# ---------------------------------------------------------------- 错误


def test_missing_explicit_config_is_an_error(tmp_path):
    missing = tmp_path / "nowhere.yaml"

    with pytest.raises(ConfigError) as excinfo:
        load_config(project_root(tmp_path), missing, environ=fake_environ(tmp_path))

    assert str(missing) in str(excinfo.value)
    assert "does not exist" in str(excinfo.value)


def test_missing_machine_and_project_files_are_not_an_error(tmp_path):
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    assert config.files == ()


@pytest.mark.parametrize(
    "text, needles",
    [
        ("roles:\n  select: {model: 3}\n", ["roles.select.model", "must be a string", "integer"]),
        ("roles:\n  select: {provider: [deepseek]}\n", ["roles.select.provider", "must be a string"]),
        ("roles:\n  select: {max_tokens: lots}\n", ["roles.select.max_tokens", "must be an integer"]),
        ("roles:\n  select: {batch_size: 0}\n", ["roles.select.batch_size", ">= 1"]),
        ("roles:\n  select: 3\n", ["roles.select", "must be a mapping"]),
        ("roles: [1, 2]\n", ["`roles`", "must be a mapping"]),
        ("providers:\n  deepseek: {base_url: 3}\n", ["providers.deepseek.base_url", "integer"]),
        ("providers:\n  deepseek: {key_env: ''}\n", ["providers.deepseek.key_env", "non-empty"]),
        ("providers: 5\n", ["`providers`", "must be a mapping"]),
        ("providers:\n  x: {base_url: https://x/v1, temperature: hot}\n", ["providers.x.temperature", "must be a number", "string"]),
        ("roles:\n  r: {provider: x, model: m, temperature: false}\n", ["roles.r.temperature", "must be a number", "boolean"]),
    ],
)
def test_a_wrong_value_type_names_the_file_and_the_key(tmp_path, text, needles):
    path = project_config(tmp_path, text)

    with pytest.raises(ConfigError) as excinfo:
        load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    message = str(excinfo.value)
    assert str(path) in message
    for needle in needles:
        assert needle in message, message


def test_invalid_yaml_and_non_mapping_top_level_are_errors(tmp_path):
    bad = project_config(tmp_path, "roles: [::\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(project_root(tmp_path), environ=fake_environ(tmp_path))
    assert str(bad) in str(excinfo.value)
    assert "not valid YAML" in str(excinfo.value)

    project_config(tmp_path, "- 1\n- 2\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(project_root(tmp_path), environ=fake_environ(tmp_path))
    assert "must be a mapping at the top level" in str(excinfo.value)


def test_an_empty_file_is_a_skipped_layer(tmp_path):
    project_config(tmp_path, "\n")
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    assert config.providers["deepseek"].base_url == "https://api.deepseek.com"


def test_an_undefined_role_needs_provider_and_model_on_the_command_line(tmp_path):
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    with pytest.raises(ConfigError) as excinfo:
        resolve_role(config, "select")
    message = str(excinfo.value)
    assert "role 'select' is not defined" in message
    assert "--provider" in message and "--model" in message
    assert str(project_root(tmp_path) / Path(*PROJECT_REL)) in message

    with pytest.raises(ConfigError):
        resolve_role(config, "select", provider="deepseek")


def test_a_role_missing_provider_or_model_says_which_key(tmp_path):
    project_config(
        tmp_path, "roles:\n  nameless: {model: m}\n  modelless: {provider: deepseek}\n"
    )
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    with pytest.raises(ConfigError) as excinfo:
        resolve_role(config, "nameless")
    assert "roles.nameless" in str(excinfo.value)
    assert "has no provider" in str(excinfo.value)

    with pytest.raises(ConfigError) as excinfo:
        resolve_role(config, "modelless")
    assert "roles.modelless" in str(excinfo.value)
    assert "has no model" in str(excinfo.value)

    # 一个都不给足就报错，给足了就放行。
    assert resolve_role(config, "modelless", model="cli").model == "cli"


def test_an_undefined_provider_names_the_file_to_edit(tmp_path):
    path = project_config(tmp_path, "roles:\n  select: {provider: moonshot, model: m}\n")
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    with pytest.raises(ConfigError) as excinfo:
        resolve_role(config, "select")

    message = str(excinfo.value)
    assert "provider 'moonshot' is not defined" in message
    assert "base_url and key_env" in message
    assert str(path) in message


def test_a_provider_missing_base_url_is_blamed_on_the_layer_that_wrote_it(tmp_path):
    machine = machine_config(
        tmp_path, "providers:\n  moonshot:\n    key_env: PRIMER_MOONSHOT_API_KEY\n"
    )
    project_config(tmp_path, "roles:\n  distill: {provider: moonshot, model: kimi}\n")
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    with pytest.raises(ConfigError) as excinfo:
        resolve_role(config, "distill")

    message = str(excinfo.value)
    assert "has no base_url" in message
    assert str(machine) in message
    assert str(project_root(tmp_path) / Path(*PROJECT_REL)) not in message


def test_override_values_are_type_checked(tmp_path):
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    with pytest.raises(ConfigError) as excinfo:
        resolve_role(config, "select", provider="deepseek", model="m", max_tokens="many")
    assert "max_tokens" in str(excinfo.value)
    assert "must be an integer" in str(excinfo.value)

    with pytest.raises(ConfigError):
        resolve_role(config, "select", provider="", model="m")


def test_every_error_message_is_ascii_only(tmp_path):
    """§2.2：异常消息一律英文。这里用"故意犯规"跑遍各类报错，一个非 ASCII 字符就红。"""

    def message(call):
        with pytest.raises(ConfigError) as excinfo:
            call()
        assert str(excinfo.value).isascii(), str(excinfo.value)
        return str(excinfo.value)

    def check_layer(text):
        """写一层配置，读它必须报错，且消息全 ASCII。"""
        project_config(tmp_path, text)
        message(lambda: load_config(project_root(tmp_path), environ=fake_environ(tmp_path)))

    # 显式文件不存在：路径由 tmp_path 拼出，另加一句英文说明。
    message(
        lambda: load_config(
            project_root(tmp_path), tmp_path / "nope.yaml", environ=fake_environ(tmp_path)
        )
    )
    check_layer("roles:\n  s: {model: 3}\n")
    check_layer("providers: 5\n")
    check_layer("providers:\n  x:\n    base_url: 3\n")

    # 解析期的各条：role 不存在、role 缺 provider/model、provider 未定义、provider 缺 base_url。
    project_config(tmp_path, "roles:\n  s: {model: m}\n")
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))
    message(lambda: resolve_role(config, "s"))
    message(lambda: resolve_role(config, "ghost"))
    message(lambda: resolve_role(config, "ghost", provider="deepseek"))

    project_config(tmp_path, "roles:\n  s: {provider: missing, model: m}\n")
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))
    message(lambda: resolve_role(config, "s"))

    project_config(tmp_path, "providers:\n  p: {}\nroles:\n  s: {provider: p, model: m}\n")
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))
    message(lambda: resolve_role(config, "s"))

    project_config(tmp_path, "roles:\n  s: {provider: deepseek, model: m}\n")
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))
    message(lambda: resolve_role(config, "s", model=3))

    # 取密钥的两条：端点没有 key_env，以及变量不在环境里。
    message(lambda: api_key(Endpoint("r", "p", "https://x", "m", ""), environ={}))
    message(lambda: api_key(Endpoint("r", "p", "https://x", "m", "PRIMER_NOPE_API_KEY"), environ={}))


# ---------------------------------------------------------------- 密钥


def moonshot_config(tmp_path) -> Config:
    project_config(
        tmp_path,
        """\
providers:
  moonshot:
    base_url: https://api.moonshot.cn/v1
    key_env: PRIMER_MOONSHOT_API_KEY
roles:
  distill: {provider: moonshot, model: kimi-k2-0905-preview}
""",
    )
    return load_config(project_root(tmp_path), environ=fake_environ(tmp_path))


def test_api_key_reads_only_the_environment_variable(tmp_path):
    endpoint = resolve_role(moonshot_config(tmp_path), "distill")

    assert api_key(endpoint, environ={"PRIMER_MOONSHOT_API_KEY": "sk-abc"}) == "sk-abc"


def test_api_key_missing_names_the_variable_and_never_the_value(tmp_path):
    endpoint = resolve_role(moonshot_config(tmp_path), "distill")

    with pytest.raises(ConfigError) as excinfo:
        api_key(endpoint, environ={})

    message = str(excinfo.value)
    assert message == (
        "environment variable PRIMER_MOONSHOT_API_KEY is not set: "
        "the endpoint's key_env names it; export it in your shell"
    )


def test_api_key_treats_a_blank_variable_as_absent(tmp_path):
    endpoint = resolve_role(moonshot_config(tmp_path), "distill")

    with pytest.raises(ConfigError) as excinfo:
        api_key(endpoint, environ={"PRIMER_MOONSHOT_API_KEY": "   "})

    assert "PRIMER_MOONSHOT_API_KEY" in str(excinfo.value)


def test_api_key_without_a_key_env_name_is_an_error(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        api_key(Endpoint("r", "p", "https://x", "m", ""), environ={})

    assert "no key_env" in str(excinfo.value)


# ---------------------------------------------------------------- --show


def test_show_report_lists_sources_providers_and_roles(tmp_path):
    machine = machine_config(tmp_path, MACHINE_YAML)
    project = project_config(tmp_path, PROJECT_YAML)
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    lines = show_report(config, environ={"MACHINE_KEY": "sk-machine-secret"})
    text = "\n".join(lines)

    assert str(machine) in text and str(project) in text
    assert "端点" in lines and "角色" in lines
    assert "deepseek：base_url=https://machine.example/v1，" in text
    assert "key_env=MACHINE_KEY（在环境里）" in text
    assert "select：provider=deepseek，model=project-model" in text
    assert "sk-machine-secret" not in text


def test_show_report_says_absent_and_never_prints_the_key(tmp_path):
    secret = "sk-top-secret-1234"
    config = moonshot_config(tmp_path)

    present = show_report(config, environ={"PRIMER_MOONSHOT_API_KEY": secret})
    absent = show_report(config, environ={})
    text = "\n".join(present)

    def provider_line(lines, name):
        return next(line for line in lines if line.strip().startswith(f"{name}："))

    assert secret not in text
    assert "PRIMER_MOONSHOT_API_KEY" in text
    assert "（在环境里）" in provider_line(present, "moonshot")
    assert "（不在环境里）" not in provider_line(present, "moonshot")
    assert "（不在环境里）" in provider_line(absent, "moonshot")


def test_show_report_flags_a_role_pointing_at_an_undefined_provider(tmp_path):
    project_config(tmp_path, "roles:\n  select: {provider: moonshot, model: m}\n")
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    text = "\n".join(show_report(config, environ={}))

    assert "provider=moonshot（provider 未定义）" in text
    assert "select：provider=moonshot（provider 未定义），model=m" in text


def test_show_report_prints_the_endpoint_temperature(tmp_path):
    project_config(
        tmp_path,
        """\
providers:
  kimi:
    base_url: https://api.kimi.com/coding/v1
    temperature: 1
roles:
  distill: {provider: kimi, model: k3}
  hot: {provider: kimi, model: k3, temperature: 0.5}
""",
    )
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    text = "\n".join(show_report(config, environ={}))

    assert "kimi：base_url=https://api.kimi.com/coding/v1，" in text
    assert "temperature=1" in text
    assert "hot：provider=kimi，model=k3，temperature=0.5" in text


def test_show_report_on_an_empty_configuration(tmp_path):
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    lines = show_report(config, environ={})
    text = "\n".join(lines)

    assert "[内置默认] providers: deepseek" in text
    assert "没有读到任何配置文件" in text
    assert "（无）" in text
    assert "base_url=https://api.deepseek.com" in text
    assert "key_env=PRIMER_DEEPSEEK_API_KEY（不在环境里）" in text


def test_show_report_prose_is_chinese_and_never_prints_a_key(tmp_path):
    """v1.1 §2.2：人读报告散文用中文（诊断与异常仍英文），任何情况下不打印密钥值。"""
    secret = "sk-prose-secret-5678"
    project_config(
        tmp_path,
        """\
providers:
  kimi:
    base_url: https://api.kimi.com/coding/v1
    temperature: 1
    key_env: PRIMER_KIMI_PROBE_KEY
roles:
  distill: {provider: kimi, model: k3}
  broken: {provider: nope, model: m}
  bare: {}
""",
    )
    config = load_config(project_root(tmp_path), environ=fake_environ(tmp_path))

    lines = show_report(config, environ={"PRIMER_KIMI_PROBE_KEY": secret})
    text = "\n".join(lines)

    def chinese(value):
        """粗判一句里有没有中文：汉字区与全角标点区都算。"""
        return any("\u3000" <= ch <= "\u9fff" for ch in value) or any(
            "\uff00" <= ch <= "\uffef" for ch in value
        )

    assert lines
    for line in lines:
        if not line.strip() or line.strip().startswith("/"):
            continue  # 空行与配置文件路径没有叙述文字
        assert chinese(line), line
    assert secret not in text
    assert "PRIMER_KIMI_PROBE_KEY" in text
    assert "（在环境里）" in text
    assert "（provider 未定义）" in text
    assert "（未设置）" in text


# ---------------------------------------------------------------- XDG


def test_xdg_config_home_is_honored(tmp_path):
    machine = machine_config(tmp_path, MACHINE_YAML)

    config = load_config(
        project_root(tmp_path), environ={"XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    )

    assert config.files == (machine,)
    assert resolve_role(config, "select").model == "machine-model"


def test_without_xdg_the_config_lives_under_home(tmp_path):
    home = tmp_path / "home"
    machine = write(home / ".config" / Path(*MACHINE_REL), MACHINE_YAML)

    config = load_config(project_root(tmp_path), environ={"HOME": str(home)})

    assert config.files == (machine,)
    assert resolve_role(config, "select").model == "machine-model"


def test_xdg_beats_home(tmp_path):
    home = tmp_path / "home"
    write(home / ".config" / Path(*MACHINE_REL), MACHINE_YAML.replace("machine-model", "home-model"))
    xdg = tmp_path / "xdg"
    write(xdg / Path(*MACHINE_REL), MACHINE_YAML)

    config = load_config(
        project_root(tmp_path), environ={"HOME": str(home), "XDG_CONFIG_HOME": str(xdg)}
    )

    assert config.files == (xdg / Path(*MACHINE_REL),)
    assert resolve_role(config, "select").model == "machine-model"


# ---------------------------------------------------------------- 命令行


def test_main_show_prints_the_report(tmp_path, capsys):
    machine_config(tmp_path, MACHINE_YAML)
    explicit = write(tmp_path / "extra.yaml", EXPLICIT_YAML)
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "MACHINE_KEY": "sk-machine-secret"}

    code = main(
        ["--show", "--project-root", str(project_root(tmp_path)), "--config", str(explicit)],
        environ=env,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "select：provider=deepseek，model=explicit-model" in out
    assert "key_env=MACHINE_KEY（在环境里）" in out
    assert "sk-machine-secret" not in out


def test_main_without_show_is_a_usage_error(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        main([], environ=fake_environ(tmp_path))

    assert excinfo.value.code == 2


def test_main_reports_a_missing_explicit_config(tmp_path, capsys):
    missing = tmp_path / "nowhere.yaml"

    code = main(
        ["--show", "--project-root", str(project_root(tmp_path)), "--config", str(missing)],
        environ=fake_environ(tmp_path),
    )

    captured = capsys.readouterr()
    assert code == 2
    assert str(missing) in captured.err
    assert captured.out == ""


def test_main_reports_a_type_error_instead_of_tracing_back(tmp_path, capsys):
    path = project_config(tmp_path, "roles:\n  s: {model: 3}\n")

    code = main(
        ["--show", "--project-root", str(project_root(tmp_path))],
        environ=fake_environ(tmp_path),
    )

    captured = capsys.readouterr()
    assert code == 2
    assert str(path) in captured.err
    assert captured.out == ""


def test_the_module_runs_as_python_dash_m(tmp_path):
    """用户真正会敲的那条命令：``python3 -m primer.config --show``。"""
    repo_root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PYTHONPATH": str(repo_root / "src"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
    }

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "primer.config",
            "--show",
            "--project-root",
            str(project_root(tmp_path)),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo_root),
    )

    assert result.returncode == 0, result.stderr
    assert "端点配置" in result.stdout
    assert "base_url=https://api.deepseek.com" in result.stdout
    assert "没有读到任何配置文件" in result.stdout
