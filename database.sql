CREATE DATABASE IF NOT EXISTS bloodconnect CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE bloodconnect;

-- Compatibility note: the legacy `phone` columns store the national ID number entered in the forms
-- (CNIC for Pakistan, Aadhaar for India, SSN for USA, ...). `country` holds the ISO code (PK, IN, US, ...).
-- Existing rows default to 'PK' because they were all registered with a Pakistani CNIC.
CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(100) NOT NULL, phone VARCHAR(30), city VARCHAR(100),
    blood_type VARCHAR(10), username VARCHAR(100) NOT NULL UNIQUE, email VARCHAR(150) NOT NULL UNIQUE,
    password VARCHAR(255) NOT NULL, is_donor BOOLEAN DEFAULT FALSE, is_admin BOOLEAN DEFAULT FALSE,
    age INT NULL, gender VARCHAR(30) NULL, health_report_path VARCHAR(500) NULL,
    health_report_status VARCHAR(20) NOT NULL DEFAULT 'pending', health_report_reviewed_at DATETIME NULL,
    is_blocked BOOLEAN NOT NULL DEFAULT FALSE, blocked_reason VARCHAR(255) NULL, blocked_at DATETIME NULL,
    country VARCHAR(2) NULL DEFAULT 'PK',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS donors (
    id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(100) NOT NULL, phone VARCHAR(30) NOT NULL,
    city VARCHAR(100) NOT NULL, country VARCHAR(2) NULL DEFAULT 'PK', address VARCHAR(255) NOT NULL, hospital VARCHAR(150) NOT NULL,
    last_donation_date DATE NULL, blood_type VARCHAR(10) NOT NULL, email VARCHAR(150) NOT NULL, age INT NULL, gender VARCHAR(30) NULL, health_report_path VARCHAR(500) NULL,
    health_report_status VARCHAR(20) NOT NULL DEFAULT 'pending', health_report_reviewed_at DATETIME NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS blood_requests (
    id INT AUTO_INCREMENT PRIMARY KEY, patient_name VARCHAR(100) NOT NULL, phone VARCHAR(30) NOT NULL,
    email VARCHAR(150) NOT NULL, blood_type VARCHAR(10) NOT NULL, units INT NOT NULL, city VARCHAR(100) NOT NULL,
    country VARCHAR(2) NULL DEFAULT 'PK',
    address VARCHAR(255) NOT NULL, hospital VARCHAR(150) NOT NULL, urgency VARCHAR(50), notes TEXT,
    status VARCHAR(30) DEFAULT 'pending', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, accepted_at TIMESTAMP NULL
);

CREATE TABLE IF NOT EXISTS patients (
    id INT PRIMARY KEY, patient_name VARCHAR(100) NOT NULL, phone VARCHAR(30), email VARCHAR(150) NOT NULL,
    blood_type VARCHAR(10), units INT, city VARCHAR(100), country VARCHAR(2) NULL DEFAULT 'PK', address VARCHAR(255), hospital VARCHAR(150),
    urgency VARCHAR(50), notes TEXT, status VARCHAR(30) DEFAULT 'accepted',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, accepted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);


CREATE TABLE IF NOT EXISTS private_messages (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    sender_id INT NOT NULL,
    receiver_id INT NOT NULL,
    message TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_read TINYINT(1) NOT NULL DEFAULT 0,
    INDEX idx_chat_pair (sender_id, receiver_id, id),
    INDEX idx_chat_unread (receiver_id, is_read, sender_id),
    CONSTRAINT fk_message_sender FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_message_receiver FOREIGN KEY (receiver_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Golden Blood (Rh-null). Status is always 'pending' on insert; only an admin can set 'verified',
-- and only with a qualified laboratory name + report reference.
CREATE TABLE IF NOT EXISTS golden_blood (
    id INT AUTO_INCREMENT PRIMARY KEY, user_id INT NOT NULL, kind VARCHAR(10) NOT NULL,
    full_name VARCHAR(100) NOT NULL, age INT NOT NULL, gender VARCHAR(30) NOT NULL,
    email VARCHAR(150) NOT NULL, address VARCHAR(255) NOT NULL, city VARCHAR(100) NOT NULL,
    country VARCHAR(2) NOT NULL, blood_group VARCHAR(20) NOT NULL,
    health_report_path VARCHAR(500) NOT NULL, blood_test_report_path VARCHAR(500) NOT NULL,
    verification_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    lab_name VARCHAR(200) NULL, lab_reference VARCHAR(120) NULL, review_note VARCHAR(500) NULL,
    reviewed_by INT NULL, reviewed_at DATETIME NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_golden_user (user_id, kind), INDEX idx_golden_status (verification_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
