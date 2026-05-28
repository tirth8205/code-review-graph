#include <eosio/asset.hpp>
#include <eosio/eosio.hpp>

using namespace eosio;

CONTRACT oracle : public contract {
public:
    using contract::contract;

    ACTION setprice(name user, uint64_t id);

    [[eosio::on_notify("*::transfer")]]
    void on_transfer(name from, name to, asset quantity, std::string memo);

    TABLE price_row {
        uint64_t id;
        uint64_t value;

        uint64_t primary_key() const { return id; }
        uint64_t by_value() const { return value; }

        EOSLIB_SERIALIZE(price_row, (id)(value))
    };

    using prices = eosio::multi_index<"prices"_n, price_row,
        indexed_by<"byvalue"_n, const_mem_fun<price_row, uint64_t, &price_row::by_value>>>;
};

ACTION oracle::setprice(name user, uint64_t id) {
    require_auth(user);

    prices price_table(get_self(), get_self().value);
    auto itr = price_table.find(id);
    if (itr == price_table.end()) {
        price_table.emplace(user, [&](auto& row) {
            row.id = id;
            row.value = 42;
        });
    } else {
        price_table.modify(itr, same_payer, [&](auto& row) {
            row.value = 43;
        });
    }

    action(
        permission_level{get_self(), "active"_n},
        "eosio.token"_n,
        "transfer"_n,
        std::make_tuple(get_self(), user, asset(1, symbol("WAX", 8)), std::string("memo"))
    ).send();
}

[[eosio::action]]
void oracle::clear(name user) {
    require_auth(get_self());
    prices price_table(get_self(), get_self().value);
    auto itr = price_table.require_find(0, "missing");
    price_table.erase(itr);
}
