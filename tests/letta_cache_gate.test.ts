import { afterEach, describe, expect, test } from "bun:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const localStoreSource = process.env.LETTA_LOCAL_STORE_SOURCE;
const temporaryDirectories: string[] = [];

afterEach(async () => {
  await Promise.all(
    temporaryDirectories
      .splice(0)
      .map((path) => rm(path, { recursive: true, force: true })),
  );
});

function userText(messages: any[]): string {
  const user = messages.find((message) => message.role === "user");
  if (!user) return "";
  return user.content
    .filter((part: any) => part.type === "text")
    .map((part: any) => part.text)
    .join("\n");
}

const maybeDescribe = localStoreSource ? describe : describe.skip;

maybeDescribe("bare Letta channel prompt cache gate", () => {
  test("disk-only edit stays cached; conversation record rewrite reloads", async () => {
    if (!localStoreSource) return;
    const { LocalStore } = await import(localStoreSource);
    const storageDir = await mkdtemp(join(tmpdir(), "local-channel-gate-"));
    temporaryDirectories.push(storageDir);
    const agentId = "agent-local-channel-gate";
    const store = new LocalStore(agentId, {
      storageDir,
      residentMessageTailLimit: 100,
    });
    const original =
      "[akid MQTT channel message]\nmessage-time: 2026-01-01 00:00\n" +
      "wake-reminder: test\nsender: User (legacy_x)\n" +
      '<channel-context>\n<members online="1">\n' +
      '<member id="legacy_x" name="User"/>\n</members>\n</channel-context>\n' +
      "message:\nhello";
    store.appendTurnInput("default", {
      agent_id: agentId,
      messages: [{ role: "user", content: original }],
    });
    store.appendStreamChunk("default", agentId, {
      message_type: "assistant_message",
      id: "provider-1",
      date: "2026-01-01T00:00:01Z",
      content: [{ type: "text", text: "reply" }],
    });
    store.appendStreamChunk("default", agentId, {
      message_type: "stop_reason",
      stop_reason: "end_turn",
    });
    expect(userText(store.listLocalMessages("default", agentId))).toBe(original);

    const conversationDir = join(
      storageDir,
      "conversations",
      Buffer.from(`default:${agentId}`).toString("base64url"),
    );
    const messagesPath = join(conversationDir, "messages.jsonl");
    const recordPath = join(conversationDir, "conversation.json");
    const stripped = original.replace(
      /<channel-context>[\s\S]*?<\/channel-context>\n?/,
      "",
    );
    const raw = await readFile(messagesPath, "utf8");
    await writeFile(
      messagesPath,
      raw.replace(JSON.stringify(original), JSON.stringify(stripped)),
    );

    expect(userText(store.listLocalMessages("default", agentId))).toBe(original);

    const record = JSON.parse(await readFile(recordPath, "utf8"));
    record.updated_at = "2026-01-01T00:00:02.001Z";
    await writeFile(recordPath, `${JSON.stringify(record, null, 2)}\n`);

    expect(userText(store.listLocalMessages("default", agentId))).toBe(stripped);
  });
});
