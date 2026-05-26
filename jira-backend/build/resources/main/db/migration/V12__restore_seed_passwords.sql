-- Seed / demo accounts keep password "123" for local username/password login even if a row was touched by OAuth flows.
UPDATE users
SET password = '123'
WHERE password IS NULL
  AND email IN ('michael@example.com', 'john@example.com', 'charles@example.com');
