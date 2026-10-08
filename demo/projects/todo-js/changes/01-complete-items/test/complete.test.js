"use strict";

const test = require("node:test");
const assert = require("node:assert");
const { TodoList } = require("../src/todo");

test("complete marks an item done", () => {
  const list = new TodoList();
  const a = list.add("a");
  list.add("b");
  assert.strictEqual(list.complete(a.id), true);
  assert.deepStrictEqual(list.open().map((i) => i.title), ["b"]);
});

test("complete returns false for unknown ids", () => {
  assert.strictEqual(new TodoList().complete(42), false);
});
