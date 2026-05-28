#include "antelope_split.hpp"

ACTION split::touch(uint64_t id) {
    pricereqs requests(get_self(), get_self().value);
    auto itr = requests.find(id);
    if (itr == requests.end()) {
        requests.emplace(get_self(), [&](auto& row) {
            row.id = id;
        });
    } else {
        requests.modify(itr, same_payer, [&](auto& row) {
            row.id = id;
        });
    }
}
