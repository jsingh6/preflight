'use strict';

// Example: user lookup with a few subtle bugs for the AI reviewer to catch.

async function getUser(db, userId) {
  const row = await db.query('SELECT * FROM users WHERE id = ?', [userId]);
  // Bug 1: no null check — row could be undefined if user doesn't exist
  return { name: row.name, email: row.email };
}

async function processUsers(db, userIds) {
  const results = [];
  for (const id of userIds) {
    const user = await getUser(db, id);
    results.push(user);
  }
  return results;
}

async function deleteUser(db, userId, requestingUserId) {
  // Bug 2: no authorization check — any caller can delete any user
  await db.query('DELETE FROM users WHERE id = ?', [userId]);
  return { deleted: userId };
}

module.exports = { getUser, processUsers, deleteUser };
