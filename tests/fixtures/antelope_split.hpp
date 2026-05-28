#include <eosio/eosio.hpp>

using namespace eosio;

CONTRACT split : public contract {
public:
    using contract::contract;

    ACTION touch(uint64_t id);

    TABLE request_row {
        uint64_t id;
        uint64_t primary_key() const { return id; }
    };

    using pricereqs = eosio::multi_index<"pricereqs"_n, request_row>;
};
