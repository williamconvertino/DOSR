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
    const item = { id: this.nextId++, title: title.trim() };
    this.items.push(item);
    return item;
  }

  remove(id) {
    const before = this.items.length;
    this.items = this.items.filter((item) => item.id !== id);
    return this.items.length < before;
  }

  all() {
    return [...this.items];
  }
}

module.exports = { TodoList };
