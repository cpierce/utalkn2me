<?php
/**
 * PHP receiver → MySQL for utalkn2me webhooks.
 *
 * Single-file drop-in for any shared-host or LAMP setup.
 * Mount at /ingest/utalkn2me (rewrite in .htaccess) or put behind
 * your framework of choice (Laravel/CakePHP/Symfony). The handler logic
 * is all in one function — lift it straight into a controller.
 *
 *   curl -X POST https://your-host/ingest/utalkn2me \
 *     -H "Authorization: Bearer $TOKEN" \
 *     -H "Content-Type: application/json" \
 *     -d '{"event":"call.upserted","uuid":"abc",...}'
 *
 * Env / ini:
 *   UTALK_TOKEN    bearer token (required)
 *   DB_DSN         e.g. mysql:host=127.0.0.1;dbname=calls;charset=utf8mb4
 *   DB_USER / DB_PASS
 *
 * MySQL schema (same as the Python example):
 *
 *   CREATE TABLE calls (
 *     uuid             CHAR(36) PRIMARY KEY,
 *     call_time        DATETIME,
 *     direction        VARCHAR(8),
 *     from_number      VARCHAR(32),
 *     to_number        VARCHAR(32),
 *     from_caller_name VARCHAR(128),
 *     duration         INT,
 *     status           VARCHAR(32),
 *     transcript_text  MEDIUMTEXT,
 *     raw              JSON,
 *     received_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
 *                                ON UPDATE CURRENT_TIMESTAMP,
 *     INDEX (call_time)
 *   );
 */
declare(strict_types=1);

header('Content-Type: application/json');

function respond(int $code, array $body): void {
    http_response_code($code);
    echo json_encode($body);
    exit;
}

// --- auth ---
$expected = getenv('UTALK_TOKEN') ?: '';
$auth = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
if ($expected && $auth !== "Bearer {$expected}") {
    respond(401, ['error' => 'unauthorized']);
}

// --- parse body ---
$payload = json_decode(file_get_contents('php://input') ?: '', true);
if (!is_array($payload) || empty($payload['uuid']) || empty($payload['event'])) {
    respond(400, ['error' => 'missing uuid or event']);
}

// --- db ---
$pdo = new PDO(
    getenv('DB_DSN') ?: 'mysql:host=127.0.0.1;dbname=calls;charset=utf8mb4',
    getenv('DB_USER') ?: 'root',
    getenv('DB_PASS') ?: '',
    [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]
);

try {
    if ($payload['event'] === 'call.upserted') {
        $sql = 'INSERT INTO calls (uuid, call_time, direction, from_number, to_number,
                                   from_caller_name, duration, status, transcript_text, raw)
                VALUES (:uuid, :time, :dir, :from, :to, :caller, :dur, :status, :tx, :raw)
                ON DUPLICATE KEY UPDATE
                    call_time        = VALUES(call_time),
                    direction        = VALUES(direction),
                    from_number      = VALUES(from_number),
                    to_number        = VALUES(to_number),
                    from_caller_name = VALUES(from_caller_name),
                    duration         = VALUES(duration),
                    status           = VALUES(status),
                    transcript_text  = COALESCE(VALUES(transcript_text), transcript_text),
                    raw              = VALUES(raw)';
        $pdo->prepare($sql)->execute([
            ':uuid'   => $payload['uuid'],
            ':time'   => $payload['time']             ?? null,
            ':dir'    => $payload['direction']        ?? null,
            ':from'   => $payload['from']             ?? null,
            ':to'     => $payload['to']               ?? null,
            ':caller' => $payload['from_caller_name'] ?? null,
            ':dur'    => $payload['duration']         ?? null,
            ':status' => $payload['status']           ?? null,
            ':tx'     => $payload['transcript']['text'] ?? null,
            ':raw'    => json_encode($payload),
        ]);
    } elseif ($payload['event'] === 'transcript.upserted') {
        $pdo->prepare('UPDATE calls SET transcript_text = :tx WHERE uuid = :uuid')
            ->execute([
                ':tx'   => $payload['transcript']['text'] ?? null,
                ':uuid' => $payload['uuid'],
            ]);
    } else {
        respond(400, ['error' => "unknown event {$payload['event']}"]);
    }
} catch (Throwable $e) {
    error_log("ingest error: {$e->getMessage()}");
    respond(500, ['error' => 'db error']);
}

respond(200, ['ok' => true, 'uuid' => $payload['uuid']]);
