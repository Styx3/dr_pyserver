import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drserver.db import game_database as db
from drserver.db.account_repository import verify_password, get_account_id, create_account

db_path = os.path.join(os.path.dirname(__file__), "..", "Database", "dungeon_runners.db")
db.initialize(db_path)

# Test 1: Create account.
aid = create_account("testuser", "testpass")
print(f"Test 1 - Create: account_id={aid}")
assert aid > 0

# Test 2: Verify correct password.
assert verify_password("testuser", "testpass"), "Correct password should verify"
print("Test 2 - Correct password: PASS")

# Test 3: Verify wrong password.
assert not verify_password("testuser", "wrongpw"), "Wrong password should NOT verify"
print("Test 3 - Wrong password: PASS")

# Test 4: Nonexistent user should return False (caller should create).
assert not verify_password("nobody", "x"), "Nonexistent user should return False"
print("Test 4 - Nonexistent user: PASS")

# Test 5: get_account_id works.
assert get_account_id("testuser") == aid
assert get_account_id("nobody") == 0
print("Test 5 - get_account_id: PASS")

# Cleanup.
db.execute_non_query("DELETE FROM accounts WHERE username = 'testuser'")
print("Cleanup done. All tests passed!")
