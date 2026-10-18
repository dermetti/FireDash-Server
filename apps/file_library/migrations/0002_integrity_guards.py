from django.db import migrations

FORWARD = """
CREATE FUNCTION file_library_entry_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ancestor record; cursor_id uuid; levels integer := 0; descendant_depth integer := 0;
BEGIN
    PERFORM 1 FROM file_library_library WHERE id = NEW.library_id FOR UPDATE;
    IF TG_OP = 'UPDATE' AND
       (NEW.library_id <> OLD.library_id OR NEW.is_folder <> OLD.is_folder OR NEW.id <> OLD.id)
    THEN RAISE EXCEPTION 'Entry identity, library and kind are immutable'; END IF;
    IF NEW.content_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM file_library_content WHERE id = NEW.content_id AND library_id = NEW.library_id
    ) THEN RAISE EXCEPTION 'Content must belong to the library'; END IF;
    cursor_id := NEW.parent_id;
    WHILE cursor_id IS NOT NULL LOOP
        IF cursor_id = NEW.id THEN RAISE EXCEPTION 'Folder cycle'; END IF;
        SELECT id, parent_id, library_id, is_folder INTO ancestor
        FROM file_library_entry WHERE id = cursor_id;
        IF NOT FOUND OR ancestor.library_id <> NEW.library_id OR NOT ancestor.is_folder
        THEN RAISE EXCEPTION 'Invalid library parent'; END IF;
        levels := levels + 1;
        IF levels > 5 THEN RAISE EXCEPTION 'Folder depth exceeded'; END IF;
        cursor_id := ancestor.parent_id;
    END LOOP;
    IF NEW.is_folder THEN
        WITH RECURSIVE descendants AS (
            SELECT id, 1 AS depth FROM file_library_entry WHERE parent_id = NEW.id AND is_folder
            UNION ALL
            SELECT e.id, d.depth + 1 FROM file_library_entry e
            JOIN descendants d ON e.parent_id = d.id WHERE e.is_folder
        ) SELECT COALESCE(MAX(depth), 0) INTO descendant_depth FROM descendants;
        IF levels + 1 + descendant_depth > 5 THEN RAISE EXCEPTION 'Folder depth exceeded'; END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER file_library_entry_integrity BEFORE INSERT OR UPDATE ON file_library_entry
FOR EACH ROW EXECUTE FUNCTION file_library_entry_guard();

CREATE FUNCTION file_library_content_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'PDF content is immutable'; END $$;
CREATE TRIGGER file_library_content_immutable BEFORE UPDATE ON file_library_content
FOR EACH ROW EXECUTE FUNCTION file_library_content_guard();

CREATE FUNCTION file_library_scope_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.department_id IS DISTINCT FROM OLD.department_id
       OR NEW.station_id IS DISTINCT FROM OLD.station_id OR NEW.id <> OLD.id
       OR NEW.source_revision < OLD.source_revision
    THEN RAISE EXCEPTION 'Library scope and revision cannot be rewritten'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER file_library_scope_immutable BEFORE UPDATE ON file_library_library
FOR EACH ROW EXECUTE FUNCTION file_library_scope_guard();
"""

REVERSE = """
DROP TRIGGER file_library_scope_immutable ON file_library_library;
DROP FUNCTION file_library_scope_guard();
DROP TRIGGER file_library_content_immutable ON file_library_content;
DROP FUNCTION file_library_content_guard();
DROP TRIGGER file_library_entry_integrity ON file_library_entry;
DROP FUNCTION file_library_entry_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("file_library", "0001_initial")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
