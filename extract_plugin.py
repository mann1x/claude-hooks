"""
Extract code-analysis plugin skills/agents/commands as standalone,
then disable the plugin to eliminate its context-consuming hooks.
"""
import json, os, shutil, sys

home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
claude_dir = os.path.join(home, ".claude")
plugin_cache = os.path.join(claude_dir, "plugins", "cache",
                            "mag-claude-plugins", "code-analysis")

if not os.path.isdir(plugin_cache):
    print(f"Plugin not found at {plugin_cache}")
    sys.exit(1)

versions = sorted([d for d in os.listdir(plugin_cache)
                    if os.path.isdir(os.path.join(plugin_cache, d))])
if not versions:
    print("No version dirs found")
    sys.exit(1)

plugin_root = os.path.join(plugin_cache, versions[-1])
print(f"Source: {plugin_root} (v{versions[-1]})")

# Use -- instead of : for Windows compatibility
SEP = "--"

# 1. Copy skills
skills_src = os.path.join(plugin_root, "skills")
skills_dst = os.path.join(claude_dir, "skills")
os.makedirs(skills_dst, exist_ok=True)
copied_skills = 0
for skill_dir in os.listdir(skills_src):
    src = os.path.join(skills_src, skill_dir)
    dst = os.path.join(skills_dst, f"code-analysis{SEP}{skill_dir}")
    if not os.path.isdir(src):
        continue
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    copied_skills += 1
print(f"Copied {copied_skills} skills to {skills_dst}")

# 2. Copy agents
agents_src = os.path.join(plugin_root, "agents")
agents_dst = os.path.join(claude_dir, "agents")
os.makedirs(agents_dst, exist_ok=True)
copied_agents = 0
for f in os.listdir(agents_src):
    src = os.path.join(agents_src, f)
    dst = os.path.join(agents_dst, f"code-analysis-{f}")
    if os.path.isfile(src):
        shutil.copy2(src, dst)
        copied_agents += 1
print(f"Copied {copied_agents} agents to {agents_dst}")

# 3. Copy commands
commands_src = os.path.join(plugin_root, "commands")
commands_dst = os.path.join(claude_dir, "commands")
os.makedirs(commands_dst, exist_ok=True)
copied_commands = 0
for f in os.listdir(commands_src):
    src = os.path.join(commands_src, f)
    dst = os.path.join(commands_dst, f"code-analysis-{f}")
    if os.path.isfile(src):
        shutil.copy2(src, dst)
        copied_commands += 1
print(f"Copied {copied_commands} commands to {commands_dst}")

# 4. Disable the plugin in settings.json
settings_path = os.path.join(claude_dir, "settings.json")
with open(settings_path) as f:
    settings = json.load(f)

plugins = settings.get("enabledPlugins", {})
if plugins.get("code-analysis@mag-claude-plugins") is not False:
    plugins["code-analysis@mag-claude-plugins"] = False
    settings["enabledPlugins"] = plugins
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
    print("Disabled code-analysis@mag-claude-plugins in settings.json")
else:
    print("Plugin already disabled")

print("\nDone. Skills/agents/commands preserved as standalone, hooks eliminated.")
