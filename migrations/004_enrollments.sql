-- 004_enrollments.sql
-- Creates: enrollments join table linking students to programs.

CREATE TABLE IF NOT EXISTS enrollments (
    student_id  BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    program_id  BIGINT NOT NULL REFERENCES programs(id) ON DELETE CASCADE,
    enrolled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (student_id, program_id)
);

CREATE INDEX IF NOT EXISTS idx_enrollments_program_id ON enrollments(program_id);
CREATE INDEX IF NOT EXISTS idx_enrollments_student_id ON enrollments(student_id);