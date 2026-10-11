---
name: mc_command
version: 1
permission: web
params:
  type: object
  properties:
    command:
      type: string
      description: One Bedrock console command, without the slash, e.g. "list", "time set day", "give Steve42 diamond 3", "tp Steve42 100 70 -40", "gamerule keepInventory true", "say Dinner time!".
  required: [command]
---
Run any Bedrock Dedicated Server console command. Commands that only look (list,
querytarget, locate, testfor, time query, weather query, gamerule with no value,
scoreboard … list, tickingarea list, allowlist list, help) run at once and return what the
server printed. Every other command — anything that changes the world, a player, the
time, the weather, a rule or the allowlist — is staged as a card Jeff approves; it runs
only then, and you are told when he does. Stop and save have their own action
(mc_server_action).
