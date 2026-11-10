create table material
(
    id   int auto_increment
        primary key,
    code varchar(50) not null,
    name varchar(50) not null,
    constraint material_pk_2
        unique (code)
);

create table material_price
(
    id          int auto_increment
        primary key,
    material_id int                                not null,
    source_id   int                                not null,
    price       decimal(10, 2)                     null comment '每吨价格（元）',
    stat_date   datetime                           not null comment '统计时间',
    created_at  datetime default CURRENT_TIMESTAMP null,
    updated_at  datetime default CURRENT_TIMESTAMP null
);

create index idx_material_price_material_id
    on material_price (material_id);

create index idx_material_price_source_id
    on material_price (source_id);

create index idx_material_price_stat_date
    on material_price (stat_date);

create table material_source
(
    id   int auto_increment
        primary key,
    url  varchar(100) not null,
    name varchar(100) null
);

