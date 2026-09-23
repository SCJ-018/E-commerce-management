-- =============================================
-- 电商后台管理系统 - 数据库建表脚本
-- 目标数据库：MySQL
-- 使用方式：mysql -h <DB_HOST> -u <DB_USER> -p < schema.sql
-- =============================================

CREATE DATABASE IF NOT EXISTS `ecommerce_admin`
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

USE `ecommerce_admin`;

-- ========================
-- 商品表
-- ========================
CREATE TABLE IF NOT EXISTS `products` (
  `id`         INT AUTO_INCREMENT PRIMARY KEY,
  `name`       VARCHAR(200)  NOT NULL COMMENT '商品名称',
  `category`   VARCHAR(100)  NOT NULL DEFAULT '' COMMENT '分类',
  `price`      DECIMAL(10,2) NOT NULL DEFAULT 0.00 COMMENT '价格',
  `stock`      INT           NOT NULL DEFAULT 0 COMMENT '库存',
  `status`     VARCHAR(20)   NOT NULL DEFAULT '在售' COMMENT '状态：在售/缺货',
  `created_at` DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品表';

-- ========================
-- 订单表
-- ========================
CREATE TABLE IF NOT EXISTS `orders` (
  `id`         VARCHAR(30)   NOT NULL PRIMARY KEY COMMENT '订单号，如 ORD20260729001',
  `customer`   VARCHAR(100)  NOT NULL COMMENT '客户姓名',
  `product`    VARCHAR(200)  NOT NULL COMMENT '商品名称',
  `qty`        INT           NOT NULL DEFAULT 1 COMMENT '数量',
  `amount`     DECIMAL(10,2) NOT NULL DEFAULT 0.00 COMMENT '金额',
  `status`     VARCHAR(20)   NOT NULL DEFAULT '待发货' COMMENT '状态：待发货/已发货/已完成/已取消',
  `date`       DATE          NOT NULL COMMENT '订单日期',
  `created_at` DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单表';

-- ========================
-- 客户表
-- ========================
CREATE TABLE IF NOT EXISTS `customers` (
  `id`         INT AUTO_INCREMENT PRIMARY KEY,
  `name`       VARCHAR(100)  NOT NULL COMMENT '姓名',
  `phone`      VARCHAR(20)   NOT NULL DEFAULT '' COMMENT '电话',
  `email`      VARCHAR(200)  NOT NULL DEFAULT '' COMMENT '邮箱',
  `address`    VARCHAR(300)  NOT NULL DEFAULT '' COMMENT '地址',
  `reg_date`   DATE          NOT NULL COMMENT '注册日期',
  `created_at` DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='客户表';

-- ========================
-- 插入初始示例数据
-- ========================

-- ========================
-- 种草监测中台
-- ========================
CREATE TABLE IF NOT EXISTS `种草收录表` (
  `id` INT AUTO_INCREMENT PRIMARY KEY,
  `发布时间` DATE NOT NULL,
  `部门` VARCHAR(50) NOT NULL DEFAULT '',
  `产品` VARCHAR(200) NOT NULL DEFAULT '',
  `发布平台` VARCHAR(30) NOT NULL DEFAULT '抖音',
  `发布渠道` VARCHAR(30) NOT NULL DEFAULT '代发',
  `笔记类型` VARCHAR(30) NOT NULL DEFAULT '种草',
  `标题` VARCHAR(500) NOT NULL DEFAULT '',
  `发布链接` VARCHAR(1000) NOT NULL DEFAULT '',
  `发布账号名称` VARCHAR(100) NOT NULL DEFAULT '',
  `发布账号ID` VARCHAR(150) NOT NULL DEFAULT '',
  `负责人` VARCHAR(100) NOT NULL DEFAULT '',
  `点赞` BIGINT NOT NULL DEFAULT 0,
  `收藏` BIGINT NOT NULL DEFAULT 0,
  `评论` BIGINT NOT NULL DEFAULT 0,
  `阅读量` BIGINT NOT NULL DEFAULT 0,
  `流量分析` MEDIUMTEXT NULL,
  `备注` VARCHAR(1000) NOT NULL DEFAULT '',
  `创建时间` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `更新时间` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX `idx_种草收录_部门日期` (`部门`, `发布时间`),
  INDEX `idx_种草收录_账号` (`发布账号名称`),
  INDEX `idx_种草收录_负责人` (`负责人`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='种草收录表';

CREATE TABLE IF NOT EXISTS `种草品类绑定表` (
  `id` INT AUTO_INCREMENT PRIMARY KEY,
  `部门` VARCHAR(50) NOT NULL,
  `店铺` VARCHAR(200) NOT NULL DEFAULT '',
  `品牌` VARCHAR(200) NOT NULL DEFAULT '',
  `品类` VARCHAR(200) NOT NULL,
  `启用` TINYINT(1) NOT NULL DEFAULT 1,
  `创建时间` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY `uk_种草绑定_部门店铺品类` (`部门`, `店铺`, `品类`),
  INDEX `idx_种草绑定_部门` (`部门`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='种草部门店铺品类绑定表';

-- 商品初始数据
INSERT INTO `products` (`name`, `category`, `price`, `stock`, `status`) VALUES
('无线蓝牙耳机 Pro', '电子产品', 299.00, 150, '在售'),
('轻薄羽绒服',       '服装鞋帽', 599.00,  80, '在售'),
('有机坚果礼盒',     '食品饮料', 128.00, 200, '在售'),
('智能台灯',         '家居生活', 189.00,  95, '在售'),
('运动跑鞋',         '服装鞋帽', 459.00,  60, '在售'),
('蓝牙音箱',         '电子产品', 199.00,   0, '缺货'),
('原味酸奶',         '食品饮料',  49.00, 300, '在售'),
('纯棉四件套',       '家居生活', 349.00,  45, '在售'),
('手机充电器',       '电子产品',  89.00, 120, '在售'),
('速干T恤',          '服装鞋帽',  99.00, 180, '在售'),
('咖啡豆',           '食品饮料', 168.00,  75, '在售'),
('空气净化器',       '家居生活', 1299.00, 20, '在售');

-- 客户初始数据
INSERT INTO `customers` (`name`, `phone`, `email`, `address`, `reg_date`) VALUES
('张三',   '13800138001', 'zhangsan@mail.com',  '北京市朝阳区',   DATE_SUB(CURDATE(), INTERVAL 30 DAY)),
('李四',   '13800138002', 'lisi@mail.com',      '上海市浦东新区', DATE_SUB(CURDATE(), INTERVAL 28 DAY)),
('王五',   '13800138003', 'wangwu@mail.com',    '广州市天河区',   DATE_SUB(CURDATE(), INTERVAL 25 DAY)),
('赵六',   '13800138004', 'zhaoliu@mail.com',   '深圳市南山区',   DATE_SUB(CURDATE(), INTERVAL 20 DAY)),
('孙七',   '13800138005', 'sunqi@mail.com',     '杭州市西湖区',   DATE_SUB(CURDATE(), INTERVAL 18 DAY)),
('周八',   '13800138006', 'zhouba@mail.com',    '成都市武侯区',   DATE_SUB(CURDATE(), INTERVAL 15 DAY)),
('吴九',   '13800138007', 'wujiu@mail.com',     '南京市玄武区',   DATE_SUB(CURDATE(), INTERVAL 12 DAY)),
('郑十',   '13800138008', 'zhengshi@mail.com',  '武汉市洪山区',   DATE_SUB(CURDATE(), INTERVAL 10 DAY)),
('钱十一', '13800138009', 'qian@mail.com',      '重庆市渝中区',   DATE_SUB(CURDATE(), INTERVAL 8 DAY)),
('刘十二', '13800138010', 'liu@mail.com',       '西安市雁塔区',   DATE_SUB(CURDATE(), INTERVAL 5 DAY));

-- 订单初始数据（订单号格式：ORD + 年月日 + 序号）
INSERT INTO `orders` (`id`, `customer`, `product`, `qty`, `amount`, `status`, `date`) VALUES
(CONCAT('ORD', DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 DAY), '%Y%m%d'), '01'),  '张三', '无线蓝牙耳机 Pro', 2, 598.00,  '已完成', DATE_SUB(CURDATE(), INTERVAL 1 DAY)),
(CONCAT('ORD', DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 2 DAY), '%Y%m%d'), '01'),  '李四', '轻薄羽绒服',       1, 599.00,  '已发货', DATE_SUB(CURDATE(), INTERVAL 2 DAY)),
(CONCAT('ORD', DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 2 DAY), '%Y%m%d'), '02'),  '王五', '有机坚果礼盒',     3, 384.00,  '待发货', DATE_SUB(CURDATE(), INTERVAL 2 DAY)),
(CONCAT('ORD', DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 3 DAY), '%Y%m%d'), '01'),  '赵六', '运动跑鞋',         1, 459.00,  '已完成', DATE_SUB(CURDATE(), INTERVAL 3 DAY)),
(CONCAT('ORD', DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 3 DAY), '%Y%m%d'), '02'),  '张三', '智能台灯',         2, 378.00,  '已发货', DATE_SUB(CURDATE(), INTERVAL 3 DAY)),
(CONCAT('ORD', DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 4 DAY), '%Y%m%d'), '01'),  '孙七', '蓝牙音箱',         1, 199.00,  '已取消', DATE_SUB(CURDATE(), INTERVAL 4 DAY)),
(CONCAT('ORD', DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 4 DAY), '%Y%m%d'), '02'),  '李四', '纯棉四件套',       1, 349.00,  '已完成', DATE_SUB(CURDATE(), INTERVAL 4 DAY)),
(CONCAT('ORD', DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 5 DAY), '%Y%m%d'), '01'),  '王五', '咖啡豆',           2, 336.00,  '待发货', DATE_SUB(CURDATE(), INTERVAL 5 DAY)),
(CONCAT('ORD', DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 5 DAY), '%Y%m%d'), '02'),  '周八', '空气净化器',       1, 1299.00, '已完成', DATE_SUB(CURDATE(), INTERVAL 5 DAY)),
(CONCAT('ORD', DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 6 DAY), '%Y%m%d'), '01'),  '赵六', '速干T恤',          5, 495.00,  '已发货', DATE_SUB(CURDATE(), INTERVAL 6 DAY)),
(CONCAT('ORD', DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 6 DAY), '%Y%m%d'), '02'),  '张三', '原味酸奶',         4, 196.00,  '待发货', DATE_SUB(CURDATE(), INTERVAL 6 DAY)),
(CONCAT('ORD', DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 7 DAY), '%Y%m%d'), '01'),  '孙七', '手机充电器',       1, 89.00,   '已完成', DATE_SUB(CURDATE(), INTERVAL 7 DAY)),
(CONCAT('ORD', DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 7 DAY), '%Y%m%d'), '02'),  '李四', '无线蓝牙耳机 Pro', 1, 299.00,  '已完成', DATE_SUB(CURDATE(), INTERVAL 7 DAY)),
(CONCAT('ORD', DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 7 DAY), '%Y%m%d'), '03'),  '王五', '有机坚果礼盒',     2, 256.00,  '已取消', DATE_SUB(CURDATE(), INTERVAL 7 DAY));
