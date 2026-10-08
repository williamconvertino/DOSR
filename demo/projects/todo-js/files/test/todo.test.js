"use strict";

const test = require("node:test");
const assert = require("node:assert");
const { TodoList } = require("../src/todo");

test("add and list", () => {
  const list = new TodoList();
  list.add("one");
  list.add("two");
  assert.deepStrictEqual(list.all().map((i) => i.title), ["one", "two"]);
});

test("remove", () => {
  const list = new TodoList();
  const item = list.add("one");
  assert.strictEqual(list.remove(item.id), true);
  assert.strictEqual(list.all().length, 0);
});

test("empty titles are rejected", () => {
  assert.throws(() => new TodoList().add("  "));
});
