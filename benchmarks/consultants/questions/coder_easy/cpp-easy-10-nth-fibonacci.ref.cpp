#include <iostream>
int main() {
    int n;
    std::cin >> n;
    long long a = 0, b = 1;
    for (int i = 0; i < n; i++) { long long t = a + b; a = b; b = t; }
    std::cout << a << std::endl;
    return 0;
}
