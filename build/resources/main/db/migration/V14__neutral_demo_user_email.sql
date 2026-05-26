-- Rename legacy demo account email/username to vendor-neutral values (was tied to a personal handle).
UPDATE users
SET username = 'alice',
    name = 'Alice',
    email = 'alice@example.com'
WHERE LOWER(email) = 'michael@example.com';
