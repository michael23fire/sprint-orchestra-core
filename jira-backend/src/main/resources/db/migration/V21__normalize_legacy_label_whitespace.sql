-- Align migrated legacy names with runtime normalization, which collapses
-- internal whitespace before case-insensitive lookup.
CREATE TEMP TABLE label_normalization_map ON COMMIT DROP AS
SELECT id,
       MIN(id) OVER (
           PARTITION BY space_id,
           REGEXP_REPLACE(LOWER(BTRIM(normalized_name)), '[[:space:]]+', ' ', 'g')
       ) AS survivor_id,
       REGEXP_REPLACE(LOWER(BTRIM(normalized_name)), '[[:space:]]+', ' ', 'g') AS new_normalized_name
FROM labels;

INSERT INTO issue_labels (issue_id, label_id)
SELECT il.issue_id, map.survivor_id
FROM issue_labels il
JOIN label_normalization_map map ON map.id = il.label_id
WHERE map.id <> map.survivor_id
ON CONFLICT (issue_id, label_id) DO NOTHING;

DELETE FROM issue_labels il
USING label_normalization_map map
WHERE il.label_id = map.id
  AND map.id <> map.survivor_id;

DELETE FROM labels label
USING label_normalization_map map
WHERE label.id = map.id
  AND map.id <> map.survivor_id;

UPDATE labels label
SET normalized_name = map.new_normalized_name
FROM label_normalization_map map
WHERE label.id = map.survivor_id
  AND label.normalized_name <> map.new_normalized_name;
