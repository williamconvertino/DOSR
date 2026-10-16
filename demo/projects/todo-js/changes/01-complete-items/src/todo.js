"use strict";

class TodoList {
  constructor() {
    this.items = [];
    this.nextId = 1;
  }

  add(title) {
    if (!title || !title.trim()) {
      throw new Error("title is required");
    }
    const item = { id: this.nextId++, title: title.trim(), done: false };
    this.items.push(item);
    return item;
  }

  remove(id) {
    const before = this.items.length;
    this.items = this.items.filter((item) => item.id !== id);
    return this.items.length < before;
  }

  complete(id) {
    const item = this.items.find((i) => i.id === id);
    if (!item) {
      return false;
    }
    item.done = true;
    return true;
  }

  open() {
    return this.items.filter((item) => !item.done);
  }

  all() {
    return [...this.items];
  }
}

module.exports = { TodoList };
