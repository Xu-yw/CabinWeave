"""
CabinWeave Skills 测试：backend/skills 下 4 份角色专属 Skill 都能被 skill_loader 加载，
agents 与角色一一对应、keywords 为空（短指令也始终注入）、各自只注入对应角色、安全节点不注入、不会被截断；
车控 Skill 的中文字数在 1300–1700 之间、其他每份不超过 2500 字；内容与代码一致：只提本角色白名单里的工具，
安全规则编号和阈值与 vehicle/safety.py 一致，故障灯分级与 vehicle/warning_lights.py 一致。

只读取仓库里的 Skill 文件，不调用模型。
"""
import re
from pathlib import Path

import pytest

from agents.agent_orchestrator import AdvisorAgent, CabinAgent, NavigationAgent, VehicleControlAgent
from agents.tools import build_agent_tools, build_rag_tools
from core.skill_loader import SkillManager
from vehicle import safety
from vehicle.service import VehicleService
from vehicle.warning_lights import WARNING_LIGHTS

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

# 目录 → (适用 Agent, Skill 名称)
EXPECTED = {
    "vehicle_control": ("vehicle_control", "车控处理规范"),
    "navigation": ("navigation", "导航处理规范"),
    "vehicle_advisor": ("advisor", "用车顾问处理规范"),
    "cabin_assistant": ("cabin", "座舱助手处理规范"),
}
LLM_AGENT_CLASSES = (VehicleControlAgent, NavigationAgent, AdvisorAgent, CabinAgent)
# Skill.to_prompt_block 的单个 Skill 截断长度
SKILL_BLOCK_MAX = 3200


def cjk_count(text):
    return len(re.findall(r"[一-鿿]", text))


@pytest.fixture(scope="module")
def manager():
    skill_manager = SkillManager(str(SKILLS_DIR))
    skill_manager.load()
    return skill_manager


@pytest.fixture(scope="module")
def skills(manager):
    return {Path(skill.path).parent.name: skill for skill in manager.skills}


def test_four_role_skills_load_with_agents_and_empty_keywords(manager, skills):
    assert manager.errors == []
    assert set(skills) == set(EXPECTED)
    assert len(manager.skills) == 4
    for directory, (agent, name) in EXPECTED.items():
        skill = skills[directory]
        assert skill.name == name
        assert skill.agents == [agent]
        assert skill.keywords == []
        assert skill.enabled is True
        assert skill.description
        assert not skill.content.startswith(f"# {name}")   # 与 name 相同的首行标题已去掉
    assert not any(Path(skill.path).name.lower() == "readme.md" for skill in manager.skills)


def test_skills_dir_has_exactly_the_four_role_skills():
    assert {path.parent.name for path in SKILLS_DIR.rglob("SKILL.md")} == set(EXPECTED)


@pytest.mark.parametrize("message", ["开窗", "有点冷", "下一首", "导航回家", ""])
def test_each_role_gets_only_its_own_skill(manager, message):
    names = {name for _, name in EXPECTED.values()}
    for agent, name in EXPECTED.values():
        prompt = manager.prompt_for(message, agent)
        assert [skill_name for skill_name in names if skill_name in prompt] == [name]
    assert manager.prompt_for(message, "safety") == ""


def test_skills_are_not_truncated(manager, skills):
    for skill in skills.values():
        assert len(skill.content) <= SKILL_BLOCK_MAX
        assert skill.to_prompt_block().endswith(skill.content.strip())
        prompt = manager.prompt_for("测试", skill.agents[0])
        assert skill.content.strip() in prompt
        assert not prompt.endswith("...")


def test_skill_lengths(skills):
    assert 1300 <= cjk_count(skills["vehicle_control"].content) <= 1700
    for directory in ("navigation", "vehicle_advisor", "cabin_assistant"):
        assert cjk_count(skills[directory].content) <= 2500


def test_skills_only_mention_tools_of_their_own_role(skills):
    registry = set(build_agent_tools(VehicleService())) | set(build_rag_tools(None))
    scopes = {cls.agent_type.value: set(cls.profile.tool_scope) for cls in LLM_AGENT_CLASSES}
    for skill in skills.values():
        mentioned = {name for name in registry if re.search(rf"\b{name}\b", skill.content)}
        agent = skill.agents[0]
        assert mentioned, skill.name
        assert mentioned <= scopes[agent], f"{skill.name} 提到了别的角色的工具: {mentioned - scopes[agent]}"


def test_vehicle_skill_matches_safety_rules(skills):
    content = skills["vehicle_control"].content
    for rule in ("S01", "S02", "S03", "S04", "S05", "S06", "S07"):
        assert rule in content
    assert f"{safety.TEMP_RANGE[0]} 到 {safety.TEMP_RANGE[1]} 度" in content
    assert f"风量 {safety.FAN_RANGE[0]} 到 {safety.FAN_RANGE[1]} 档" in content
    assert f"座椅加热 {safety.SEAT_HEAT_RANGE[0]} 到 {safety.SEAT_HEAT_RANGE[1]} 档" in content
    assert f"超过 {safety.HIGHWAY_SPEED_KMH} 公里" in content and f"{safety.HIGHWAY_OPEN_LIMIT}%" in content
    assert f"电量低于 {safety.LOW_BATTERY_PCT}%" in content
    assert "暂不支持" in content and "driver_preferences" in content


def test_cabin_skill_volume_matches_safety_range(skills):
    content = skills["cabin_assistant"].content
    assert f"音量范围 {safety.VOLUME_RANGE[0]} 到 {safety.VOLUME_RANGE[1]}" in content
    assert "delta 为 3 或 -3" in content
    assert "模拟拨号" in content


def test_advisor_skill_severity_levels_match_warning_table(skills):
    content = skills["vehicle_advisor"].content
    lines = {severity: next(line for line in content.splitlines() if line.startswith(f"- {severity}"))
             for severity in ("紧急", "严重", "注意")}
    for item in WARNING_LIGHTS:
        line = lines[item["severity"]]
        assert item["name"] in line, f"{item['name']} 应列在{item['severity']}一级"
        assert item["color"] in line
    assert "以车辆用户手册为准" in content


def test_navigation_skill_keeps_addresses_private(skills):
    content = skills["navigation"].content
    assert "不念具体住址" in content
    assert "need_info" in content and "最多播报 3 个" in content
    assert "空闲桩" in content and "功率" in content and "距离" in content
