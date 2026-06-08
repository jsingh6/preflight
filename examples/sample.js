'use strict';

async function getUser(db, userId) {
  const row = await db.query('SELECT * FROM users WHERE id = ?', [userId]);
  if (!row) return null;
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
  if (!requestingUserId) throw new Error('requestingUserId is required');
  const requester = await getUser(db, requestingUserId);
  if (!requester) throw new Error('Requesting user not found');
  if (requestingUserId !== userId && !requester.isAdmin) {
    throw new Error('Unauthorized: only admins can delete other users');
  }
  await db.query('DELETE FROM users WHERE id = ?', [userId]);
  return { deleted: userId };
}

module.exports = { getUser, processUsers, deleteUser };
